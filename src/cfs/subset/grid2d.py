# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Bounding-box subsetting for curvilinear / projected grids with 2-D lat/lon.

Many high-resolution forcing products are on projected grids — RDRS/CaSR on a
rotated pole, CONUS404/HRRR on Lambert Conformal Conic — where ``latitude`` and
``longitude`` are 2-D auxiliary coordinates over native index dimensions
(``rlat/rlon``, ``y/x``, …). The regular 1-D ``.sel(slice)`` logic in
:mod:`cfs.subset.bbox` does not apply; instead we build a boolean mask over the
2-D lat/lon arrays, take the enclosing index window, and ``isel`` the native
dims. A point-scale bbox (smaller than a grid cell) falls back to the single
nearest cell.

This is the model-agnostic core of SYMFLUENCE's RDRS handler, generalized to
arbitrary coordinate/dimension names so any 2-D-grid connector can reuse it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog

from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox

logger = structlog.get_logger(__name__)


def _native_dims(ds: Any, lat_name: str, lon_name: str) -> tuple[str, str]:
    """Return the two index dims that the 2-D lat coordinate spans (y, x)."""
    dims = ds[lat_name].dims
    if len(dims) != 2:
        raise SubsetError(
            f"grid2d expects a 2-D '{lat_name}' coordinate; got dims {dims}. "
            "Use cfs.subset.bbox for regular 1-D grids."
        )
    return dims[0], dims[1]


def subset_2d_grid(
    ds: Any,
    bbox: BoundingBox,
    *,
    lat_name: str = "lat",
    lon_name: str = "lon",
    buffer: int = 2,
) -> Any:
    """Subset a 2-D-grid dataset to ``bbox`` via an index window on the native dims.

    Returns the ``isel``-ed dataset (a contiguous block of the projected grid
    covering the bbox, plus ``buffer`` cells on each side). Raises
    :class:`SubsetError` only if the grid and bbox do not overlap at all.
    """
    lat = np.asarray(ds[lat_name].values)
    lon = np.asarray(ds[lon_name].values)
    y_dim, x_dim = _native_dims(ds, lat_name, lon_name)
    y_slice, x_slice = bbox_index_window(lat, lon, bbox, buffer=buffer)
    return ds.isel({y_dim: y_slice, x_dim: x_slice})


def bbox_index_window(
    lat: Any,
    lon: Any,
    bbox: BoundingBox,
    *,
    buffer: int = 2,
) -> tuple[slice, slice]:
    """Return ``(y_slice, x_slice)`` index windows into a 2-D lat/lon grid for ``bbox``.

    Separated from :func:`subset_2d_grid` so a connector whose variables live in
    *separate* stores from the grid (e.g. HRRR's per-variable zarr groups, which
    carry no lat/lon) can compute the window once from a shared grid file and
    ``isel`` every variable with it. Snaps to the nearest cell for a sub-cell bbox.
    """
    lat = np.asarray(lat)
    lon = np.asarray(lon)

    # Normalize a 0–360 grid longitude to the bbox's signed convention so the
    # mask comparison is apples-to-apples.
    if lon.max() > 180.0 and (bbox.min_lon < 0 or bbox.max_lon < 0):
        lon = ((lon + 180.0) % 360.0) - 180.0

    mask = (
        (lat >= bbox.min_lat) & (lat <= bbox.max_lat)
        & (lon >= bbox.min_lon) & (lon <= bbox.max_lon)
    )
    y_idx, x_idx = np.where(mask)

    if len(y_idx) > 0:
        y_min, y_max = int(y_idx.min()), int(y_idx.max())
        x_min, x_max = int(x_idx.min()), int(x_idx.max())
    else:
        # Sub-cell / point bbox: snap to the single nearest grid cell.
        clat = (bbox.min_lat + bbox.max_lat) / 2.0
        clon = (bbox.min_lon + bbox.max_lon) / 2.0
        dist = np.sqrt((lat - clat) ** 2 + (lon - clon) ** 2)
        if not np.isfinite(dist).any():
            raise SubsetError("2-D grid has no finite lat/lon near the requested bbox")
        y_near, x_near = np.unravel_index(int(np.nanargmin(dist)), dist.shape)
        y_min = y_max = int(y_near)
        x_min = x_max = int(x_near)
        logger.debug("grid2d sub-cell bbox; snapped to nearest cell", y=y_min, x=x_min)

    ny, nx = lat.shape
    return (
        slice(max(0, y_min - buffer), min(ny - 1, y_max + buffer) + 1),
        slice(max(0, x_min - buffer), min(nx - 1, x_max + buffer) + 1),
    )
