# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NOAA nClimGrid-Daily connector via NCEI THREDDS OPeNDAP."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

NCEI_ROOT = "https://www.ncei.noaa.gov/thredds/dodsC/nclimgrid-daily"

_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tavg", CanonicalVar.AIR_TEMPERATURE, offset=273.15, note="nClimGrid daily tavg (degC) -> K"),
    VariableMapping(
        "prcp",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 86400.0,
        note="nClimGrid daily precipitation (mm/day) -> flux (kg m-2 s-1)",
    ),
]


def _monthly_url(year: int, month: int) -> str:
    return f"{NCEI_ROOT}/{year}/ncdd-{year}{month:02d}-grd-scaled.nc"


def _months(time_range: TimeRange) -> list[tuple[int, int]]:
    y, m = time_range.start.year, time_range.start.month
    end_y, end_m = time_range.end.year, time_range.end.month
    out: list[tuple[int, int]] = []
    while (y, m) <= (end_y, end_m):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _open_anonymous_opendap(url: str):
    try:
        import pydap  # noqa: F401
        import xarray as xr
    except ImportError as e:  # pragma: no cover
        raise MissingExtraError(
            "Anonymous OPeNDAP access needs the 'earthdata' extra for pydap: "
            "pip install -e '.[earthdata]'"
        ) from e
    return xr.open_dataset(url, engine="pydap")


@register("nclimgrid_daily")
class NClimGridDailyConnector(BaseForcingConnector):
    slug = "nclimgrid_daily"
    display_name = "NOAA nClimGrid-Daily (5 km, CONUS)"
    base_url = NCEI_ROOT
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="NOAA nClimGrid-Daily temperature and precipitation (5 km)",
                description=(
                    "NOAA NCEI nClimGrid-Daily gridded CONUS temperature and "
                    "precipitation, opened through NCEI THREDDS OPeNDAP."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.0416667,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-125.0, min_lat=24.0, max_lon=-66.0, max_lat=50.0),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="NOAA/NCEI public data.",
                citation="NOAA NCEI, nClimGrid-Daily gridded climate dataset.",
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

        wanted = set(variables) if variables else None
        keep = [m.source_name for m in _MAPPINGS if wanted is None or m.canonical in wanted]
        if not keep:
            raise SubsetError("None of the requested variables are offered by nClimGrid-Daily")

        pieces = []
        for year, month in _months(time_range):
            ds = _open_anonymous_opendap(_monthly_url(year, month))
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            pieces.append(ds[keep].sel(time=slice(time_range.start, time_range.end)))

        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No nClimGrid-Daily data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="NOAA nClimGrid-Daily via NCEI THREDDS OPeNDAP; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
