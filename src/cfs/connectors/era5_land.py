# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ERA5-Land connector — ECMWF ERA5-Land via the Copernicus CDS API.

ERA5-Land is 0.1° hourly land reanalysis, retrieved from the CDS by request
(``reanalysis-era5-land``). The CDS ``area`` parameter does the spatial subset
server-side, so this connector builds monthly requests, downloads the NetCDFs,
concatenates them, de-accumulates the daily-reset flux fields, harmonizes to the
canonical schema, and trims to the exact time range.

The ERA5-Land precip/radiation gotcha: ``tp``/``ssrd``/``strd`` are accumulated
from 00 UTC and reset each day. They must be de-accumulated (see
:mod:`cfs.subset.deaccumulate`) *before* the unit conversion — handled here by
``deaccumulate=True`` on those mappings.

Auth-gated: needs CDS credentials (``~/.cdsapirc`` or ``CDSAPI_URL``/
``CDSAPI_KEY``). Without them, ``fetch`` raises a clear RegistrationRequiredError.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import timedelta

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

logger = structlog.get_logger()

CDS_DATASET = "reanalysis-era5-land"


@dataclass(frozen=True)
class _LandVar:
    """A ERA5-Land variable: CDS request name, NetCDF short name, and mapping."""

    request_name: str  # name passed in the CDS request
    nc_name: str       # variable name in the downloaded NetCDF
    canonical: CanonicalVar
    scale: float = 1.0
    deaccumulate: bool = False
    note: str = ""


# tp/ssrd/strd are daily-reset accumulations → de-accumulate, then convert:
#   tp:   m of water / hour-step  → kg m-2 s-1  via *1000/3600
#   ssrd/strd: J m-2 / hour-step  → W m-2       via /3600
_VARS: list[_LandVar] = [
    _LandVar("2m_temperature", "t2m", CanonicalVar.AIR_TEMPERATURE),
    _LandVar("2m_dewpoint_temperature", "d2m", CanonicalVar.DEWPOINT_TEMPERATURE),
    _LandVar("surface_pressure", "sp", CanonicalVar.SURFACE_AIR_PRESSURE),
    _LandVar("10m_u_component_of_wind", "u10", CanonicalVar.EASTWARD_WIND),
    _LandVar("10m_v_component_of_wind", "v10", CanonicalVar.NORTHWARD_WIND),
    _LandVar(
        "total_precipitation", "tp", CanonicalVar.PRECIPITATION_FLUX,
        scale=1000.0 / 3600.0, deaccumulate=True,
        note="ERA5-Land daily-reset accumulation (m) -> flux (kg m-2 s-1)",
    ),
    _LandVar(
        "surface_solar_radiation_downwards", "ssrd", CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        scale=1.0 / 3600.0, deaccumulate=True,
        note="ERA5-Land daily-reset accumulation (J m-2) -> flux (W m-2)",
    ),
    _LandVar(
        "surface_thermal_radiation_downwards", "strd", CanonicalVar.LONGWAVE_RADIATION_DOWN,
        scale=1.0 / 3600.0, deaccumulate=True,
        note="ERA5-Land daily-reset accumulation (J m-2) -> flux (W m-2)",
    ),
]


def _mappings_for(selected: list[_LandVar]) -> list[VariableMapping]:
    return [
        VariableMapping(
            v.nc_name, v.canonical, scale=v.scale, deaccumulate=v.deaccumulate, note=v.note
        )
        for v in selected
    ]


# Full mapping table (NetCDF short names → canonical), exposed for tests.
_MAPPINGS: list[VariableMapping] = _mappings_for(_VARS)


@register("era5_land")
class ERA5LandConnector(CDSAPIMixin, BaseForcingConnector):
    slug = "era5_land"
    display_name = "ECMWF ERA5-Land (0.1°, hourly, via CDS)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:hourly",
                provider=self.slug,
                name="ERA5-Land hourly (0.1°)",
                description=(
                    "ECMWF ERA5-Land hourly land-surface reanalysis, retrieved "
                    "from the Copernicus CDS with server-side bbox (area) subset."
                ),
                variables=[
                    ProductVariable(canonical=v.canonical, source_name=v.nc_name)
                    for v in _VARS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="Copernicus / ECMWF licence (free, attribution; accept on CDS).",
                citation="Muñoz-Sabater et al. (2021), ERA5-Land, ESSD 13:4349-4383.",
            )
        ]

    def _cache_name(self, bbox: BoundingBox, req_names: list[str], year: int, month: int) -> str:
        key = f"{bbox.min_lon},{bbox.min_lat},{bbox.max_lon},{bbox.max_lat}|{','.join(sorted(req_names))}"
        h = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:8]
        return f"{self.slug}_{year}{month:02d}_{h}.nc"

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
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by ERA5-Land")
        req_names = [v.request_name for v in selected]
        area = self._cds_area(bbox)
        cache = self._cds_cache_dir()

        # ERA5-Land accumulations (tp/ssrd/strd) reset at 00 UTC, and the 00:00
        # step holds the *whole previous day's* total. De-accumulation therefore
        # needs the hour before the window to give the first real step a valid
        # predecessor — otherwise its raw daily total leaks through (QC catches
        # it as an out-of-range flux). Pad the chunk range back one hour, which
        # pulls the prior month only when the window starts at a month boundary.
        needs_pad = any(v.deaccumulate for v in selected)
        chunk_range = (
            TimeRange(start=time_range.start - timedelta(hours=1), end=time_range.end)
            if needs_pad else time_range
        )

        pieces = []
        for year, month in self._month_chunks(chunk_range):
            request = {
                "variable": req_names,
                "year": str(year),
                "month": f"{month:02d}",
                "day": self._all_days(),
                "time": self._hourly_times(),
                "data_format": "netcdf",
                "download_format": "unarchived",
                "area": area,
            }
            target = cache / self._cache_name(bbox, req_names, year, month)
            await self._cds_retrieve(CDS_DATASET, request, target)
            pieces.append(self._cds_open(target))

        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        # Harmonize (de-accumulates tp/ssrd/strd over the full series), then trim
        # to the exact requested window so end-month padding is dropped.
        canonical = harmonize(ds_all, _mappings_for(selected), requested=variables)
        canonical = canonical.sel(time=slice(time_range.start, time_range.end))
        if canonical.sizes.get("time", 0) == 0:
            raise SubsetError(
                f"No ERA5-Land data in [{time_range.start}, {time_range.end}]"
            )

        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"ERA5-Land via CDS ({CDS_DATASET}); area subset; de-accumulated; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
