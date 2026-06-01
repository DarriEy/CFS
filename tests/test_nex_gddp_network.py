# SPDX-License-Identifier: MIT
"""Live NEX-GDDP-CMIP6 integration test — anonymous S3. Marked 'network'."""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")
pytest.importorskip("s3fs")
pytest.importorskip("h5py")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar

COLORADO = BoundingBox(min_lon=-106.0, min_lat=39.8, max_lon=-105.5, max_lat=40.2)


@pytest.mark.network
async def test_nex_gddp_historical_and_ssp585():
    discover()
    conn = get_connector("nex_gddp")()  # default model ACCESS-CM2 / r1i1p1f1
    vars_ = [CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.WIND_SPEED]

    async with conn:
        hist, hr = await conn.fetch(
            "nex_gddp:historical", COLORADO,
            TimeRange(start=datetime(2010, 6, 1), end=datetime(2010, 6, 3)), vars_)
        hist = hist.load()
        ssp, sr = await conn.fetch(
            "nex_gddp:ssp585", COLORADO,
            TimeRange(start=datetime(2090, 6, 1), end=datetime(2090, 6, 3)), vars_)
        ssp = ssp.load()

    for ds, res, scen in ((hist, hr, "historical"), (ssp, sr, "ssp585")):
        assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                                     CanonicalVar.WIND_SPEED}
        assert 230.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 325.0
        assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0
        assert ds.attrs["cmip6_scenario"] == scen
        assert ds.attrs["cmip6_model"] == "ACCESS-CM2"
        assert not res.warnings
