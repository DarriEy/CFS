# SPDX-License-Identifier: MIT
"""Live ECMWF open-data integration test — ecmwf-forecasts AWS mirror. Marked 'network'.

Uses a recent cycle from the rolling ~few-day open-data window; skips cleanly if
that cycle has already aged off the mirror.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("xarray")
pytest.importorskip("cfgrib")

from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_ecmwf_ifs_multistep_deaccum():
    discover()
    conn_cls = get_connector("ecmwf_opendata")
    bbox = BoundingBox(min_lon=-105.6, min_lat=39.5, max_lon=-105.1, max_lat=40.0)  # Colorado
    # Yesterday 00z (well inside the rolling window); steps 3/6/9 from one cycle.
    cyc = (datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0,
                                              microsecond=0) - timedelta(days=1))
    tr = TimeRange(start=cyc + timedelta(hours=3), end=cyc + timedelta(hours=9))

    async with conn_cls() as conn:
        try:
            ds, result = await conn.fetch(
                "ecmwf_opendata:ifs_0p25", bbox, tr,
                variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.DEWPOINT_TEMPERATURE,
                           CanonicalVar.PRECIPITATION_FLUX,
                           CanonicalVar.SHORTWAVE_RADIATION_DOWN],
            )
            ds = ds.load()
        except SubsetError as e:
            pytest.skip(f"ECMWF open-data cycle aged off the mirror: {e}")

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.DEWPOINT_TEMPERATURE,
                                 CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.SHORTWAVE_RADIATION_DOWN}
    assert result.n_times == 3
    assert f"cycle {cyc:%Y%m%d} 00z" in result.provenance
    # 0-360 grid longitudes normalized to the signed bbox.
    assert -106.0 < float(ds["longitude"].mean()) < -104.0
    t = ds[CanonicalVar.AIR_TEMPERATURE].values
    td = ds[CanonicalVar.DEWPOINT_TEMPERATURE].values
    assert np.nanmin(t) > 230.0 and np.nanmax(t) < 330.0
    assert np.all(td <= t + 0.5)                                   # dewpoint <= temperature
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0  # de-accumulated flux
    assert float(ds[CanonicalVar.SHORTWAVE_RADIATION_DOWN].min()) >= 0.0


@pytest.mark.network
async def test_ecmwf_aifs_multistep_deaccum():
    # AIFS-single (ML model): 6-hourly steps to 360 h, same forcing + de-accumulation.
    discover()
    conn_cls = get_connector("ecmwf_opendata")
    bbox = BoundingBox(min_lon=-105.6, min_lat=39.5, max_lon=-105.1, max_lat=40.0)
    cyc = (datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0,
                                     microsecond=0) - timedelta(days=1))
    tr = TimeRange(start=cyc + timedelta(hours=6), end=cyc + timedelta(hours=18))  # steps 6,12,18

    async with conn_cls() as conn:
        try:
            ds, result = await conn.fetch(
                "ecmwf_opendata:aifs_0p25", bbox, tr,
                variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                           CanonicalVar.SHORTWAVE_RADIATION_DOWN],
            )
            ds = ds.load()
        except SubsetError as e:
            pytest.skip(f"ECMWF AIFS cycle aged off the mirror: {e}")

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                                 CanonicalVar.SHORTWAVE_RADIATION_DOWN}
    assert result.n_times == 3                                    # 6/12/18h from one cycle
    assert "aifs-single" in result.provenance
    assert -106.0 < float(ds["longitude"].mean()) < -104.0
    t = ds[CanonicalVar.AIR_TEMPERATURE].values
    assert np.nanmin(t) > 230.0 and np.nanmax(t) < 330.0
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0


@pytest.mark.network
async def test_ecmwf_ens_member_dimension():
    # ENS (enfo): a `member` dimension; restrict to 3 members to keep the test light.
    discover()
    conn_cls = get_connector("ecmwf_opendata")
    bbox = BoundingBox(min_lon=-105.6, min_lat=39.5, max_lon=-105.1, max_lat=40.0)
    cyc = (datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0,
                                     microsecond=0) - timedelta(days=1))
    tr = TimeRange(start=cyc + timedelta(hours=3), end=cyc + timedelta(hours=6))  # steps 3,6

    async with conn_cls(config={"members": [1, 2, 3]}) as conn:
        try:
            ds, result = await conn.fetch(
                "ecmwf_opendata:enfo_0p25", bbox, tr,
                variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
            )
            ds = ds.load()
        except SubsetError as e:
            pytest.skip(f"ECMWF ENS cycle aged off the mirror: {e}")

    assert "member" in ds.dims
    assert list(ds["member"].values) == [1, 2, 3]
    assert result.n_times == 2
    assert "enfo" in result.provenance and "3 members" in result.provenance
    # Ensemble spread: members are distinct realisations, not copies.
    t = ds[CanonicalVar.AIR_TEMPERATURE]
    assert not np.allclose(t.sel(member=1).values, t.sel(member=2).values)
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0  # de-accumulated per member
