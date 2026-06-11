# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""EM-Earth connector — global daily met reanalysis on AWS S3 (credential-gated).

EM-Earth (Tang et al. 2022) is a 0.1° global daily product (1950-2019) merging
gauge + reanalysis, in deterministic and probabilistic variants, on the
``emearth`` S3 bucket as per-variable, per-month NetCDFs.

⚠ Two caveats, both confirmed by probing:

1. **Access.** The ``emearth`` bucket currently allows anonymous *listing* but
   *denies anonymous GET* (every region, even requester-pays) — it needs AWS
   credentials. So this connector defaults to anonymous and raises a clear error
   pointing at that; pass ``config={"anon": False}`` to use the standard AWS
   credential chain once you have bucket access. (SYMFLUENCE's handler assumed
   anonymous access and silently produced "no data" when it 403s.)

2. **Precip units UNVERIFIED.** EM-Earth daily ``prcp`` is *assumed* mm/day
   (→ ``/86400``), but this could not be confirmed against a file (no read
   access), and a unit error here would NOT be caught by range QC (mm/day and
   mm/h both fall in the valid flux range). Every fetch that includes
   precipitation therefore carries an explicit warning. Temperature is safe —
   a °C-vs-K mistake blows the canonical range and QC flags it.
"""

from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.core.config import get_settings
from cfs.core.exceptions import MissingExtraError, RegistrationRequiredError, SubsetError
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

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

BUCKET = "emearth"
VARIANTS = {"deterministic": "nc/deterministic_raw_daily", "probabilistic": "nc/probabilistic_daily"}

# tmean/tdew are °C → +273.15 (QC-protected). prcp is BEST-EFFORT mm/day → /86400.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tmean", CanonicalVar.AIR_TEMPERATURE, offset=273.15),
    VariableMapping("tdew", CanonicalVar.DEWPOINT_TEMPERATURE, offset=273.15),
    VariableMapping(
        "prcp", CanonicalVar.PRECIPITATION_FLUX, scale=1.0 / 86400.0,
        note="EM-Earth daily prcp ASSUMED mm/day -> flux (UNVERIFIED)",
    ),
]
# canonical → source var the mapping reads (for request-driven file selection).
_SRC = {m.canonical: m.source_name for m in _MAPPINGS}
_PRECIP_WARNING = (
    "EM-Earth precipitation units are assumed mm/day (unverified — confirm against "
    "a file before scientific use)"
)


@register("em_earth")
class EMEarthConnector(BaseForcingConnector):
    slug = "em_earth"
    display_name = "EM-Earth (0.1° daily, global; AWS S3, credential-gated)"
    base_url = f"s3://{BUCKET}"
    protocol = "s3_direct"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = self.config or {}
        self.anon = cfg.get("anon", True)
        self.variant = cfg.get("variant", "deterministic")
        if self.variant not in VARIANTS:
            raise SubsetError(f"Unknown EM-Earth variant '{self.variant}' (deterministic/probabilistic)")
        self._fs = None

    def _filesystem(self):
        if self._fs is None:
            try:
                import s3fs
            except ImportError as e:  # pragma: no cover
                raise MissingExtraError("EM-Earth needs s3fs (the 'climate' extra).") from e
            self._fs = s3fs.S3FileSystem(anon=self.anon)
        return self._fs

    async def list_products(self) -> list[ForcingProduct]:
        from datetime import datetime

        return [
            ForcingProduct(
                id=f"{self.slug}:{self.variant}_daily",
                provider=self.slug,
                name=f"EM-Earth {self.variant} daily (0.1°, global)",
                description=(
                    f"EM-Earth {self.variant} daily met (temperature, dewpoint, "
                    "precipitation). AWS bucket is credential-gated; precip units "
                    "unverified (see connector docstring)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(
                    start=datetime(1950, 1, 1), end=datetime(2019, 12, 31),
                    resolution=TemporalResolution.DAILY,
                ),
                protocol=Protocol.S3_DIRECT,
                license="EM-Earth (cite Tang et al. 2022); bucket access may be restricted.",
                citation="Tang et al. (2022), EM-Earth, BAMS 103:E996-E1018.",
            )
        ]

    def _key(self, var: str, year: int, month: int) -> str:
        folder = VARIANTS[self.variant]
        fname = f"EM_Earth_{self.variant}_daily_{var}_{year}{month:02d}.nc"
        return f"{BUCKET}/{folder}/{var}/{fname}"

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else {m.canonical for m in _MAPPINGS}
        selected = [m for m in _MAPPINGS if m.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by EM-Earth")
        src_vars = [m.source_name for m in selected]

        fs = self._filesystem()
        warnings: list[str] = []
        if CanonicalVar.PRECIPITATION_FLUX in wanted:
            warnings.append(_PRECIP_WARNING)

        # (year, month) chunks over the request, clamped to the 1950-2019 record.
        months = pd.date_range(
            pd.Timestamp(time_range.start).to_period("M").to_timestamp(),
            pd.Timestamp(time_range.end).to_period("M").to_timestamp(),
            freq="MS",
        )
        per_var_concat = {}
        for var in src_vars:
            chunks = []
            for ms in months:
                if not (1950 <= ms.year <= 2019):
                    continue
                key = self._key(var, ms.year, ms.month)
                try:
                    raw = fs.cat_file(key)
                except PermissionError as e:
                    raise RegistrationRequiredError(
                        self.slug,
                        "https://www.frdr-dfdr.ca/ (EM-Earth dataset)",
                        "The 'emearth' S3 bucket denied access. It is no longer anonymously "
                        "readable — provide AWS credentials with bucket access (config={'anon': "
                        "False} + standard AWS credential chain), or obtain EM-Earth from FRDR.",
                    ) from e
                except (FileNotFoundError, OSError):
                    continue
                ds = xr.open_dataset(io.BytesIO(raw), engine="h5netcdf")[[var]]
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
                ds = ds.sel(time=slice(time_range.start, time_range.end))
                if ds.sizes.get("time", 0) > 0:
                    chunks.append(ds.load())
            if chunks:
                per_var_concat[var] = xr.concat(chunks, dim="time")

        if not per_var_concat:
            raise SubsetError(f"No EM-Earth data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.merge(list(per_var_concat.values()), join="inner")

        canonical = harmonize(ds_all, selected, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"EM-Earth {self.variant} S3 (credential-gated); canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings,
        )
