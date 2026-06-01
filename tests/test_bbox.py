# SPDX-License-Identifier: MIT
"""Spatial subsetting tests — coordinate conventions, no network needed."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset


def _grid(lats, lons):
    data = np.zeros((len(lats), len(lons)))
    return xr.Dataset(
        {"x": (("latitude", "longitude"), data)},
        coords={"latitude": np.array(lats), "longitude": np.array(lons)},
    )


def test_descending_lat_0_360_lon():
    # ERA5-like: latitude descending, longitude 0..360.
    ds = _grid([51.0, 50.5, 50.0, 49.5], [244.0, 245.0, 246.0, 247.0])
    # bbox in -180..180: lon -115 -> 245 in 0..360.
    bbox = BoundingBox(min_lon=-116.0, min_lat=50.0, max_lon=-114.0, max_lat=51.0)
    plan = plan_bbox_subset(ds, bbox)
    assert plan.lat_descending is True
    assert plan.uses_0_360 is True
    out = apply_bbox_subset(ds, plan)
    assert out.sizes["latitude"] > 0
    assert out.sizes["longitude"] > 0
    assert float(out.longitude.min()) >= 244.0


def test_ascending_lat_signed_lon():
    ds = _grid([49.5, 50.0, 50.5, 51.0], [-116.0, -115.0, -114.0, -113.0])
    bbox = BoundingBox(min_lon=-115.5, min_lat=50.0, max_lon=-114.0, max_lat=51.0)
    plan = plan_bbox_subset(ds, bbox)
    assert plan.lat_descending is False
    assert plan.uses_0_360 is False
    out = apply_bbox_subset(ds, plan)
    assert out.sizes["latitude"] == 3  # 50.0, 50.5, 51.0
    assert out.sizes["longitude"] > 0


def test_subcell_bbox_expands_not_empty():
    ds = _grid([51.0, 50.75, 50.5], [245.0, 245.25, 245.5])
    # A near-point bbox far smaller than the 0.25° grid.
    bbox = BoundingBox(min_lon=-114.8, min_lat=50.74, max_lon=-114.79, max_lat=50.76)
    plan = plan_bbox_subset(ds, bbox)
    out = apply_bbox_subset(ds, plan)
    assert out.sizes["latitude"] >= 1
    assert out.sizes["longitude"] >= 1


def test_antimeridian_wrap():
    # 0..360 grid spanning the dateline; request 179E..-179E (=179..181).
    ds = _grid([1.0, 0.0, -1.0], [178.0, 179.0, 180.0, 181.0, 182.0])
    bbox = BoundingBox(min_lon=179.0, min_lat=-1.0, max_lon=-179.0, max_lat=1.0)
    assert bbox.crosses_antimeridian is True
    plan = plan_bbox_subset(ds, bbox)
    assert plan.wrap_longitude is True
    out = apply_bbox_subset(ds, plan)
    assert out.sizes["longitude"] > 0


def test_empty_selection_raises():
    ds = _grid([51.0, 50.5], [245.0, 245.5])
    # Latitude band entirely outside the grid.
    bbox = BoundingBox(min_lon=-115.0, min_lat=10.0, max_lon=-114.0, max_lat=11.0)
    plan = plan_bbox_subset(ds, bbox)
    with pytest.raises(SubsetError):
        apply_bbox_subset(ds, plan)
