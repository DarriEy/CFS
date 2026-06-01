# SPDX-License-Identifier: MIT
"""ERA5 ARCO integration test — hits Google Cloud, so marked 'network'.

Run with:  pytest -m network
Skipped by default:  pytest -m 'not network'
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")
pytest.importorskip("gcsfs")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_fetch_small_bbox_returns_canonical_cube():
    discover()
    conn_cls = get_connector("era5_arco")
    bbox = BoundingBox(min_lon=-114.5, min_lat=50.7, max_lon=-114.0, max_lat=51.1)
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6))

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "era5_arco:single_levels", bbox, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
        )

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX}
    assert ds[CanonicalVar.AIR_TEMPERATURE].attrs["units"] == "K"
    assert result.n_times == 7  # hourly, inclusive 00..06
    assert result.lazy is True
    assert "latitude" in ds.coords and "longitude" in ds.coords
