# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NEX-GDDP-CMIP6 connector — NASA downscaled CMIP6 climate projections (S3).

The first *projection* connector: bias-corrected, 0.25° daily-downscaled CMIP6
output on the public ``nex-gddp-cmip6`` S3 bucket (anonymous). Unlike a single
reanalysis, a projection has a **model × scenario × ensemble** axis:

  * **scenario** is exposed as the product id — one product per SSP plus
    ``historical`` (``nex_gddp:ssp585``, ``nex_gddp:historical``, …);
  * **model** and **ensemble member** are connector ``config`` knobs, defaulting
    to ACCESS-CM2 / r1i1p1f1, so the full native catalogue is reachable:
    ``get_connector("nex_gddp")(config={"model": "MPI-ESM1-2-HR", "member": "r1i1p1f1"})``.

The native files are CF/SI, so all mappings are identity (``pr`` is already a
mass flux). The chosen model/scenario/member are recorded in
``FetchResult.provenance`` and on the returned dataset's attrs.

File names vary by per-model grid label (``gn``/``gr``/``gr1``) and carry an
optional ``_v2.0`` correction suffix, so the connector *lists* each variable
directory and resolves the file per year (preferring ``_v2.0``) rather than
hardcoding a name. Layout/units/coords were probe-confirmed. Anonymous →
live-verifiable.
"""

from __future__ import annotations

import re
import time

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.core.config import get_settings
from cfs.core.exceptions import MissingExtraError, SubsetError
from cfs.core.models import (
    BoundingBox,
    FetchResult,
    ForcingProduct,
    ProductVariable,
    Protocol,
    TemporalExtent,
    TemporalResolution,
    TimeRange,
)
from cfs.core.registry import register
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

BUCKET = "nex-gddp-cmip6/NEX-GDDP-CMIP6"
DEFAULT_MODEL = "ACCESS-CM2"
DEFAULT_MEMBER = "r1i1p1f1"

# scenario → (start_year, end_year) advertised extent.
SCENARIOS = {
    "historical": (1950, 2014),
    "ssp126": (2015, 2100),
    "ssp245": (2015, 2100),
    "ssp370": (2015, 2100),
    "ssp585": (2015, 2100),
}

# NEX-GDDP-CMIP6 native var → canonical. All CF/SI → identity.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tas", CanonicalVar.AIR_TEMPERATURE),            # K
    VariableMapping("pr", CanonicalVar.PRECIPITATION_FLUX),          # already kg m-2 s-1
    VariableMapping("huss", CanonicalVar.SPECIFIC_HUMIDITY),         # kg/kg
    VariableMapping("sfcWind", CanonicalVar.WIND_SPEED),             # m/s
    VariableMapping("rsds", CanonicalVar.SHORTWAVE_RADIATION_DOWN),  # W/m2
    VariableMapping("rlds", CanonicalVar.LONGWAVE_RADIATION_DOWN),   # W/m2
]
_YEAR_RE = re.compile(r"_(\d{4})(?:_v[\d.]+)?\.nc$")


@register("nex_gddp")
class NEXGDDPConnector(BaseForcingConnector):
    slug = "nex_gddp"
    display_name = "NASA NEX-GDDP-CMIP6 (0.25° daily downscaled CMIP6)"
    base_url = f"s3://{BUCKET}"
    protocol = "s3_direct"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.model = (self.config or {}).get("model", DEFAULT_MODEL)
        self.member = (self.config or {}).get("member", DEFAULT_MEMBER)
        self._fs = None

    def _filesystem(self):
        if self._fs is None:
            try:
                import s3fs
            except ImportError as e:  # pragma: no cover
                raise MissingExtraError("NEX-GDDP needs s3fs (the 'climate' extra).") from e
            self._fs = s3fs.S3FileSystem(anon=True)
        return self._fs

    async def list_products(self) -> list[ForcingProduct]:
        from datetime import datetime

        products = []
        for scenario, (y0, y1) in SCENARIOS.items():
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{scenario}",
                    provider=self.slug,
                    name=f"NEX-GDDP-CMIP6 {scenario} daily (0.25°)",
                    description=(
                        f"NASA NEX-GDDP-CMIP6 downscaled CMIP6 {scenario} projection "
                        f"(model={self.model}, member={self.member}); SSP/historical "
                        "selected via product id, model/member via connector config."
                    ),
                    variables=[
                        ProductVariable(canonical=m.canonical, source_name=m.source_name)
                        for m in _MAPPINGS
                    ],
                    resolution_deg=0.25,
                    crs="EPSG:4326",
                    bbox=BoundingBox(min_lon=-180, min_lat=-60, max_lon=180, max_lat=90),
                    temporal=TemporalExtent(
                        start=datetime(y0, 1, 1), end=datetime(y1, 12, 31),
                        resolution=TemporalResolution.DAILY,
                    ),
                    protocol=Protocol.S3_DIRECT,
                    license="NASA NEX (open); cite the source CMIP6 model + NEX-GDDP.",
                    citation="Thrasher et al. (2022), NEX-GDDP-CMIP6, Scientific Data 9:262.",
                )
            )
        return products

    def _resolve_year_files(self, var: str, scenario: str) -> dict[int, str]:
        """Map year → S3 path for ``var``, preferring the ``_v2.0`` correction."""
        fs = self._filesystem()
        prefix = f"{BUCKET}/{self.model}/{scenario}/{self.member}/{var}"
        out: dict[int, str] = {}
        try:
            listing = fs.ls(prefix)
        except FileNotFoundError:
            return out
        for path in listing:
            name = path.split("/")[-1]
            m = _YEAR_RE.search(name)
            if not m:
                continue
            year = int(m.group(1))
            is_v2 = "_v" in name
            if year not in out or is_v2:
                out[year] = path
        return out

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[object, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        scenario = product_id.split(":", 1)[1]
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else {m.canonical for m in _MAPPINGS}
        selected = [m for m in _MAPPINGS if m.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by NEX-GDDP")

        fs = self._filesystem()
        file_maps = {m.source_name: self._resolve_year_files(m.source_name, scenario) for m in selected}
        warnings: list[str] = []
        years = range(time_range.start.year, time_range.end.year + 1)
        pieces = []
        for year in years:
            per_var = []
            for m in selected:
                path = file_maps[m.source_name].get(year)
                if path is None:
                    warnings.append(f"{m.source_name} {year} not available for {self.model}/{scenario}")
                    continue
                ds = xr.open_dataset(fs.open(path), engine="h5netcdf", chunks={})[[m.source_name]]
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
                ds = ds.sel(time=slice(time_range.start, time_range.end))
                if ds.sizes.get("time", 0) > 0:
                    per_var.append(ds.load())
            if per_var:
                pieces.append(xr.merge(per_var, join="inner"))

        if not pieces:
            raise SubsetError(
                f"No NEX-GDDP data in [{time_range.start}, {time_range.end}] for "
                f"{self.model}/{scenario}/{self.member}"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, selected, requested=variables, lat_name="lat", lon_name="lon")
        canonical.attrs.update(
            {"cmip6_model": self.model, "cmip6_scenario": scenario, "cmip6_member": self.member}
        )
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"NEX-GDDP-CMIP6 {self.model}/{scenario}/{self.member} (S3); "
                "bbox+time subset; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings,
        )
