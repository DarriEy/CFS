# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Generic, provider-agnostic spatial subsetting of a gridded forcing cube.

This is the genuinely reusable "acquire-and-subset" geometry logic, lifted out
of SYMFLUENCE's ERA5 handler and made connector-neutral: bbox→grid expansion,
0–360 vs −180–180 longitude normalization, antimeridian wrapping, and
descending-latitude handling. Any Zarr/OPeNDAP forcing source on a regular
lat/lon grid can reuse it.

It does *not* know about variable names, units, or any model schema — it only
slices coordinates. Harmonization lives in :mod:`cfs.subset.canonical`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox

logger = structlog.get_logger(__name__)


@dataclass
class GridSubsetPlan:
    """Resolved, grid-aware slicing instructions for one bbox on one dataset."""

    lat_min: float
    lat_max: float
    lon_min: float  # in the dataset's own longitude convention
    lon_max: float
    wrap_longitude: bool
    lat_descending: bool
    lon_dataset_min: float
    lon_dataset_max: float
    lat_resolution: float
    lon_resolution: float
    uses_0_360: bool


def _coord_resolution(values: Any) -> float:
    if len(values) < 2:
        return 0.25  # sensible default for a single-cell axis
    return float(abs(values[1] - values[0]))


def plan_bbox_subset(
    ds: Any,
    bbox: BoundingBox,
    lat_name: str = "latitude",
    lon_name: str = "longitude",
) -> GridSubsetPlan:
    """Work out how to slice ``ds`` for ``bbox`` without selecting anything yet.

    Detects the longitude convention (0–360 or −180–180), expands a sub-cell
    bbox to at least one grid cell, and flags antimeridian wrapping. Returns a
    plan that :func:`apply_bbox_subset` consumes.
    """
    lats = ds[lat_name].values
    lons = ds[lon_name].values
    lat_res = _coord_resolution(lats)
    lon_res = _coord_resolution(lons)
    lat_descending = bool(len(lats) > 1 and lats[0] > lats[-1])

    lon_dataset_min = float(lons.min())
    lon_dataset_max = float(lons.max())
    uses_0_360 = bool(lon_dataset_max > 180.0)

    lat_min, lat_max = bbox.min_lat, bbox.max_lat
    req_lon_min, req_lon_max = bbox.min_lon, bbox.max_lon

    # Expand a bbox narrower than one grid cell so the slice isn't empty.
    if (lat_max - lat_min) < lat_res:
        mid = (lat_min + lat_max) / 2.0
        lat_min, lat_max = mid - lat_res / 2.0, mid + lat_res / 2.0
        logger.debug("expanded latitude bbox to one grid cell", lat_min=lat_min, lat_max=lat_max)

    def _to_dataset_lon(lon: float) -> float:
        return lon % 360.0 if uses_0_360 else lon

    lon_min = _to_dataset_lon(req_lon_min)
    lon_max = _to_dataset_lon(req_lon_max)

    # Antimeridian: either the request itself crossed it, or normalization to
    # 0–360 made min > max (e.g. -5..5 -> 355..5).
    wrap_longitude = bool(bbox.crosses_antimeridian or (uses_0_360 and lon_min > lon_max))

    if abs(lon_max - lon_min) < lon_res and not wrap_longitude:
        mid = (lon_min + lon_max) / 2.0
        lon_min, lon_max = mid - lon_res / 2.0, mid + lon_res / 2.0

    return GridSubsetPlan(
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
        wrap_longitude=wrap_longitude,
        lat_descending=lat_descending,
        lon_dataset_min=lon_dataset_min,
        lon_dataset_max=lon_dataset_max,
        lat_resolution=lat_res,
        lon_resolution=lon_res,
        uses_0_360=uses_0_360,
    )


def apply_bbox_subset(
    ds: Any,
    plan: GridSubsetPlan,
    lat_name: str = "latitude",
    lon_name: str = "longitude",
) -> Any:
    """Apply a :class:`GridSubsetPlan` to ``ds``, returning the spatial subset.

    Handles descending latitude and antimeridian wrapping (two slices, then
    concat + sort). Raises :class:`SubsetError` if the result is empty.
    """
    import xarray as xr

    lat_slice = (
        slice(plan.lat_max, plan.lat_min) if plan.lat_descending else slice(plan.lat_min, plan.lat_max)
    )
    ds_lat = ds.sel({lat_name: lat_slice})

    if plan.wrap_longitude and plan.lon_min > plan.lon_max:
        left = ds_lat.sel({lon_name: slice(plan.lon_min, plan.lon_dataset_max)})
        right = ds_lat.sel({lon_name: slice(plan.lon_dataset_min, plan.lon_max)})
        out = xr.concat([left, right], dim=lon_name).sortby(lon_name)
    else:
        out = ds_lat.sel({lon_name: slice(plan.lon_min, plan.lon_max)})

    if out.sizes.get(lat_name, 0) == 0 or out.sizes.get(lon_name, 0) == 0:
        raise SubsetError(
            f"bbox selected an empty grid (lat={out.sizes.get(lat_name, 0)}, "
            f"lon={out.sizes.get(lon_name, 0)}); check coordinate conventions"
        )
    return out
