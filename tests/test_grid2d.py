# SPDX-License-Identifier: MIT
"""2-D / projected-grid subsetting tests (curvilinear lat/lon), no network."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox
from cfs.subset.grid2d import bbox_index_window, subset_2d_grid


def _curvilinear(ny=20, nx=24, lat0=45.0, lon0=-120.0, dlat=0.1, dlon=0.1, skew=0.02):
    """A small rotated/curvilinear grid with 2-D lat/lon over (y, x) dims."""
    y = np.arange(ny)
    x = np.arange(nx)
    yy, xx = np.meshgrid(y, x, indexing="ij")
    lat = lat0 + dlat * yy + skew * xx
    lon = lon0 + dlon * xx + skew * yy
    data = np.zeros((ny, nx))
    return xr.Dataset(
        {"t": (("y", "x"), data)},
        coords={
            "lat": (("y", "x"), lat),
            "lon": (("y", "x"), lon),
        },
    )


def test_window_covers_bbox():
    ds = _curvilinear()
    bbox = BoundingBox(min_lon=-119.0, min_lat=45.5, max_lon=-118.0, max_lat=46.2)
    out = subset_2d_grid(ds, bbox, buffer=0)
    # Every selected cell's lat/lon range must envelope the bbox interior.
    assert float(out.lat.max()) >= bbox.max_lat
    assert float(out.lat.min()) <= bbox.min_lat
    assert out.sizes["y"] < ds.sizes["y"]  # actually subset, not whole grid
    assert out.sizes["x"] < ds.sizes["x"]


def test_buffer_expands_window():
    ds = _curvilinear()
    bbox = BoundingBox(min_lon=-119.0, min_lat=45.5, max_lon=-118.5, max_lat=45.9)
    small = subset_2d_grid(ds, bbox, buffer=0)
    big = subset_2d_grid(ds, bbox, buffer=2)
    assert big.sizes["y"] >= small.sizes["y"]
    assert big.sizes["x"] >= small.sizes["x"]


def test_subcell_bbox_snaps_to_nearest():
    ds = _curvilinear()
    # A bbox far smaller than a 0.1° cell.
    bbox = BoundingBox(min_lon=-119.001, min_lat=45.999, max_lon=-119.0, max_lat=46.0)
    out = subset_2d_grid(ds, bbox, buffer=0)
    assert out.sizes["y"] == 1
    assert out.sizes["x"] == 1


def test_0_360_grid_normalized():
    # Same grid but longitudes expressed in 0–360 convention.
    ds = _curvilinear(lon0=240.0)  # -120 -> 240
    bbox = BoundingBox(min_lon=-119.0, min_lat=45.5, max_lon=-118.0, max_lat=46.2)
    out = subset_2d_grid(ds, bbox, buffer=0)
    assert out.sizes["y"] >= 1 and out.sizes["x"] >= 1


def test_bbox_index_window_matches_subset():
    # The standalone window helper (used by HRRR for coordinate-less var groups)
    # must select the same block subset_2d_grid would.
    ds = _curvilinear()
    bbox = BoundingBox(min_lon=-119.0, min_lat=45.5, max_lon=-118.0, max_lat=46.2)
    ys, xs = bbox_index_window(ds.lat.values, ds.lon.values, bbox, buffer=0)
    direct = subset_2d_grid(ds, bbox, buffer=0)
    assert direct.sizes["y"] == ys.stop - ys.start
    assert direct.sizes["x"] == xs.stop - xs.start


def test_rejects_1d_coord():
    ds = xr.Dataset(
        {"t": (("lat", "lon"), np.zeros((3, 3)))},
        coords={"lat": [1.0, 2.0, 3.0], "lon": [1.0, 2.0, 3.0]},
    )
    bbox = BoundingBox(min_lon=1.0, min_lat=1.0, max_lon=2.0, max_lat=2.0)
    with pytest.raises(SubsetError):
        subset_2d_grid(ds, bbox)
