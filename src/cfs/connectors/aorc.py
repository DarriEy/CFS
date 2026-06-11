# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""AORC connector — NOAA Analysis of Record for Calibration v1.1 (S3 Zarr).

Migrated from SYMFLUENCE's lat-lon AORC path: one public, anonymous Zarr archive
per year on ``noaa-nws-aorc-v1-1-1km``, on a regular ~1 km lat/lon grid over
CONUS. CFS opens each year covering the request, subsets to bbox + time, and
concatenates — returning the canonical cube. The NWM-projected fallback path in
SYMFLUENCE (a 2-D LCC grid) is intentionally left for a separate connector once
its variable names are confirmed.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.zarr_store import ZarrStoreMixin
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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

AORC_BUCKET = "noaa-nws-aorc-v1-1-1km"  # one {year}.zarr per year

# Native AORC v1.1 names → canonical. All fields are instantaneous SI except
# APCP_surface, an hourly precip accumulation in kg m-2 (≡ mm) → /3600 = flux.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("TMP_2maboveground", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("SPFH_2maboveground", CanonicalVar.SPECIFIC_HUMIDITY),
    VariableMapping("PRES_surface", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping("DSWRF_surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("DLWRF_surface", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    VariableMapping("UGRD_10maboveground", CanonicalVar.EASTWARD_WIND),
    VariableMapping("VGRD_10maboveground", CanonicalVar.NORTHWARD_WIND),
    VariableMapping(
        "APCP_surface",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 3600.0,
        note="AORC hourly accumulation (kg m-2) -> flux (kg m-2 s-1)",
    ),
]


@register("aorc")
class AORCConnector(ZarrStoreMixin, BaseForcingConnector):
    slug = "aorc"
    display_name = "NOAA AORC v1.1 (1 km, hourly)"
    base_url = f"s3://{AORC_BUCKET}"
    protocol = "zarr"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:conus_1km",
                provider=self.slug,
                name="AORC v1.1 CONUS forcing (1 km, hourly)",
                description=(
                    "NOAA Analysis of Record for Calibration, lat-lon gridded "
                    "v1.1, from the public NODD Zarr archives (one per year)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.00833,  # ~1 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-130.0, min_lat=20.0, max_lon=-60.0, max_lat=53.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.ZARR,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NWS Analysis of Record for Calibration (AORC) v1.1.",
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

        years = range(time_range.start.year, time_range.end.year + 1)
        pieces = []
        for year in years:
            ds = self._open_s3_zarr(f"{AORC_BUCKET}/{year}.zarr", anonymous=True)
            ds = ds.assign_coords(
                longitude=ds.longitude.load(), latitude=ds.latitude.load()
            )
            plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
            ds_sp = apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude")
            ds_sp = ds_sp.sel(time=slice(time_range.start, time_range.end))
            if ds_sp.sizes.get("time", 0) > 0:
                pieces.append(ds_sp)

        if not pieces:
            raise SubsetError(
                f"No AORC data in [{time_range.start}, {time_range.end}] for the bbox"
            )
        ds_all = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"AORC v1.1 S3 Zarr ({AORC_BUCKET}); per-year subset+concat; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
