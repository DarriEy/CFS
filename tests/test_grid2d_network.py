# SPDX-License-Identifier: MIT
"""Live 2-D-grid integration tests — RDRS (OPeNDAP) and CONUS404 (OSN Zarr).

Both anonymous, so genuinely verifiable. Marked 'network'.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_rdrs_fetch_small_bbox():
    discover()
    conn_cls = get_connector("rdrs")
    bbox = BoundingBox(min_lon=-114.4, min_lat=50.8, max_lon=-113.9, max_lat=51.2)  # Calgary
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 3))

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "rdrs:casr_v32", bbox, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
        )
        ds = ds.load()

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX}
    assert ds[CanonicalVar.AIR_TEMPERATURE].attrs["units"] == "K"
    # 2-D grid: latitude/longitude are 2-D coords; native dims are rlat/rlon.
    assert result.n_lat > 0 and result.n_lon > 0
    assert result.n_times == 4  # hourly 00..03
    assert "latitude" in ds.coords and ds["latitude"].ndim == 2
    # Plausible June temperature and a non-negative precip flux.
    assert 250.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 320.0
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0
    assert not result.warnings  # QC clean


@pytest.mark.network
async def test_conus404_fetch_small_bbox():
    pytest.importorskip("s3fs")
    discover()
    conn_cls = get_connector("conus404")
    bbox = BoundingBox(min_lon=-106.0, min_lat=39.8, max_lon=-105.5, max_lat=40.2)  # Colorado
    tr = TimeRange(start=datetime(2015, 6, 1, 1), end=datetime(2015, 6, 1, 3))

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "conus404:hourly", bbox, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SHORTWAVE_RADIATION_DOWN],
        )
        ds = ds.load()

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SHORTWAVE_RADIATION_DOWN}
    assert result.n_times == 3  # 01..03 (pad step dropped)
    assert result.n_lat > 0 and result.n_lon > 0
    # De-accumulated shortwave must be a sane flux, never the raw J m-2 accumulation.
    sw = ds[CanonicalVar.SHORTWAVE_RADIATION_DOWN]
    assert float(sw.min()) >= 0.0
    assert float(sw.max()) < 1500.0
    assert not result.warnings  # QC clean — proves de-accumulation worked
