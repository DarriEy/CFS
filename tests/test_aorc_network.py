# SPDX-License-Identifier: MIT
"""AORC S3 integration test — hits NOAA Open Data S3, marked 'network'."""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")
pytest.importorskip("s3fs")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_aorc_fetch_small_bbox():
    discover()
    conn_cls = get_connector("aorc")
    bbox = BoundingBox(min_lon=-114.5, min_lat=50.7, max_lon=-114.0, max_lat=51.1)
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 5))

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "aorc:conus_1km", bbox, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
        )
        ds = ds.load()

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX}
    assert ds[CanonicalVar.AIR_TEMPERATURE].attrs["units"] == "K"
    assert result.n_times == 6  # hourly 00..05 inclusive
    assert result.n_lat > 0 and result.n_lon > 0
    # Precip flux is a small positive rate, never a raw mm accumulation.
    pmax = float(ds[CanonicalVar.PRECIPITATION_FLUX].max())
    assert 0.0 <= pmax < 0.05
