# SPDX-License-Identifier: MIT
"""CHIRPS integration test — opens the 1.1 GB yearly global over HTTP byte-range
and pulls only the chunks overlapping a small bbox + short window, so it's fast
despite the file size. Hits data.chc.ucsb.edu, hence marked 'network'.

Run with:  pytest -m network
Skipped by default:  pytest -m 'not network'
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")
pytest.importorskip("h5netcdf")
pytest.importorskip("aiohttp")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_chirps_byterange_subset_is_small_and_canonical():
    discover()
    conn_cls = get_connector("chirps")
    # India monsoon window — in CHIRPS coverage (50S–50N) and reliably wet.
    bbox = BoundingBox(min_lon=73.0, min_lat=18.0, max_lon=74.0, max_lat=19.0)
    tr = TimeRange(start=datetime(2015, 7, 1), end=datetime(2015, 7, 5))

    async with conn_cls() as conn:
        ds, result = await conn.fetch("chirps:daily_p05", bbox, tr)

    assert set(ds.data_vars) == {CanonicalVar.PRECIPITATION_FLUX}
    assert ds[CanonicalVar.PRECIPITATION_FLUX].attrs["units"] == "kg m-2 s-1"
    assert result.n_times == 5  # daily, inclusive 1..5 July
    assert result.n_lat > 0 and result.n_lon > 0
    assert "latitude" in ds.coords and "longitude" in ds.coords
    # Monsoon: at least some positive flux in the window (sanity, not a unit check).
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].max()) > 0.0
