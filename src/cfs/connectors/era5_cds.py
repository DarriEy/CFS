# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ERA5 CDS connector — ECMWF ERA5 reanalysis via direct CDS API.

Direct access to ECMWF ERA5 single-level reanalysis (``reanalysis-era5-single-levels``)
via the Copernicus CDS API. This provides a robust alternative to ARCO-ERA5
for users with CDS credentials.

Auth-gated: needs CDS credentials (``~/.cdsapirc`` or ``CDSAPI_URL``/``CDSAPI_KEY``).
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.cds_api import CDSAPIMixin
from cfs.core.config import get_settings
from cfs.core.exceptions import SubsetError
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
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

CDS_DATASET = "reanalysis-era5-single-levels"

# (CDS request name, canonical).
_VARS = [
    ("2m_temperature", CanonicalVar.AIR_TEMPERATURE),
    ("2m_dewpoint_temperature", CanonicalVar.DEWPOINT_TEMPERATURE),
    ("surface_pressure", CanonicalVar.SURFACE_AIR_PRESSURE),
    ("10m_u_component_of_wind", CanonicalVar.EASTWARD_WIND),
    ("10m_v_component_of_wind", CanonicalVar.NORTHWARD_WIND),
    ("total_precipitation", CanonicalVar.PRECIPITATION_FLUX),
    ("surface_solar_radiation_downwards", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    ("surface_thermal_radiation_downwards", CanonicalVar.LONGWAVE_RADIATION_DOWN),
]

# NetCDF short names in ERA5 results.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("t2m", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("d2m", CanonicalVar.DEWPOINT_TEMPERATURE),
    VariableMapping("sp", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping("u10", CanonicalVar.EASTWARD_WIND),
    VariableMapping("v10", CanonicalVar.NORTHWARD_WIND),
    VariableMapping("tp", CanonicalVar.PRECIPITATION_FLUX, scale=1000.0/3600.0,
                    note="ERA5 hourly accumulation (m water) -> mass flux (kg m-2 s-1)"),
    VariableMapping("ssrd", CanonicalVar.SHORTWAVE_RADIATION_DOWN, scale=1.0/3600.0),
    VariableMapping("strd", CanonicalVar.LONGWAVE_RADIATION_DOWN, scale=1.0/3600.0),
]


def _extract_ncs(zip_path: Path, dest: Path) -> list[Path]:
    """Extract every .nc member of a CDS zip into ``dest`` (idempotent)."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.lower().endswith(".nc"):
                target = dest / Path(member).name
                if not (target.exists() and target.stat().st_size > 0):
                    with zf.open(member) as src, open(target, "wb") as fh:
                        fh.write(src.read())
                out.append(target)
    return out


@register("era5_cds")
class ERA5CDSConnector(CDSAPIMixin, BaseForcingConnector):
    slug = "era5_cds"
    display_name = "ECMWF ERA5 (0.25°, hourly, via CDS)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:single_levels",
                provider=self.slug,
                name="ERA5 single-levels reanalysis",
                description=(
                    "ECMWF ERA5 single-level hourly reanalysis from the Copernicus CDS."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="Copernicus / ECMWF licence (free, attribution; accept on CDS).",
                citation="Hersbach et al. (2020), ERA5, QJRMS 146:1999-2049.",
            )
        ]

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else {m.canonical for m in _MAPPINGS}
        selected_vars = [v[0] for v in _VARS if v[1] in wanted]
        if not selected_vars:
            raise SubsetError("None of the requested variables are offered by ERA5-CDS")

        cache = self._cds_cache_dir()
        extract_dir = cache / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        pieces = []

        # Chunk by month.
        for year, month in self._month_chunks(time_range):
            request = {
                "product_type": "reanalysis",
                "data_format": "netcdf",
                "download_format": "zip",
                "variable": selected_vars,
                "year": str(year),
                "month": f"{month:02d}",
                "day": self._all_days(),
                "time": self._hourly_times(),
                "area": self._cds_area(bbox),
            }
            # The modern CDS returns ERA5 single-levels as a ZIP that splits the
            # request by step type — instantaneous fields (t2m/d2m/sp/u10/v10) and
            # accumulations (tp/ssrd/strd) land in separate NetCDFs. Extract both
            # and merge them onto the shared hourly grid.
            target = cache / f"{self.slug}_{year}{month:02d}.zip"
            await self._cds_retrieve(CDS_DATASET, request, target)
            ncs = _extract_ncs(target, extract_dir / f"{year}{month:02d}")
            if not ncs:
                raise SubsetError(f"ERA5-CDS archive for {year}-{month:02d} held no .nc file")
            parts = [self._cds_open(p) for p in ncs]
            ds = xr.merge(parts, join="inner") if len(parts) > 1 else parts[0]
            ds = ds.sel(time=slice(time_range.start, time_range.end))
            if ds.sizes.get("time", 0) > 0:
                pieces.append(ds)

        if not pieces:
            raise SubsetError(f"No ERA5 data in [{time_range.start}, {time_range.end}]")
            
        ds_all = xr.concat(pieces, dim="time").sortby("time")

        # ERA5 single-levels accumulations (tp/ssrd/strd) are per-hour totals — each
        # step holds the accumulation over the *preceding* hour, not a running total
        # that resets (that's ERA5-Land). So a plain unit conversion is correct here;
        # no reset-aware de-accumulation is needed (mirrors the era5_arco connector).
        canonical = harmonize(ds_all, _MAPPINGS, requested=variables)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="ERA5 single-levels via CDS API; area subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
