# SPDX-License-Identifier: MIT
"""NEX-GDDP-CMIP6 offline tests: products-per-scenario, config knobs, mappings."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.nex_gddp import _MAPPINGS, SCENARIOS, NEXGDDPConnector
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "nex_gddp" in list_providers()


async def test_one_product_per_scenario():
    conn = NEXGDDPConnector()
    products = await conn.list_products()
    ids = {p.id for p in products}
    assert ids == {f"nex_gddp:{s}" for s in SCENARIOS}


def test_defaults_and_config_knobs():
    assert NEXGDDPConnector().model == "ACCESS-CM2"
    assert NEXGDDPConnector().member == "r1i1p1f1"
    c = NEXGDDPConnector(config={"model": "MPI-ESM1-2-HR", "member": "r1i1p1f1"})
    assert c.model == "MPI-ESM1-2-HR"


def test_all_identity_cf_si():
    # NEX-GDDP is native CF/SI — every mapping is a pass-through.
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    canon = {m.canonical for m in _MAPPINGS}
    assert {CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.AIR_TEMPERATURE,
            CanonicalVar.WIND_SPEED, CanonicalVar.SPECIFIC_HUMIDITY} <= canon


def test_file_version_parsing():
    from cfs.connectors.nex_gddp import _file_version

    assert _file_version("tas_day_X_gn_2014.nc") == (0,)
    assert _file_version("tas_day_X_gn_2014_v1.1.nc") == (1, 1)
    assert _file_version("tas_day_X_gn_2014_v2.0.nc") == (2, 0)
    assert _file_version("tas_day_X_gn_2014_v10.0.nc") == (10, 0)


def test_year_file_resolution_picks_highest_version(monkeypatch):
    # The dir-listing resolver must pick the highest numeric _vN correction:
    # unsuffixed < _v1.1 < _v2.0 < _v10.0 — regardless of listing order
    # ("_v10.0" sorts lexicographically BEFORE "_v2.0", the old last-match trap).
    stem = "b/tas/tas_day_ACCESS-CM2_historical_r1i1p1f1_gn"
    listing = [
        f"{stem}_2014_v10.0.nc",   # hypothetical: lexicographically before _v2.0
        f"{stem}_2014_v2.0.nc",
        f"{stem}_2014_v1.1.nc",
        f"{stem}_2014.nc",
        f"{stem}_2013_v1.1.nc",
        f"{stem}_2013.nc",
        f"{stem}_2012.nc",
    ]

    class FakeFS:
        def ls(self, prefix):
            return listing

    conn = NEXGDDPConnector()
    monkeypatch.setattr(conn, "_filesystem", lambda: FakeFS())
    out = conn._resolve_year_files("tas", "historical")
    assert out[2014].endswith("_2014_v10.0.nc")
    assert out[2013].endswith("_2013_v1.1.nc")
    assert out[2012].endswith("_2012.nc")


def test_year_file_resolution_missing_prefix(monkeypatch):
    class FakeFS:
        def ls(self, prefix):
            raise FileNotFoundError(prefix)

    conn = NEXGDDPConnector()
    monkeypatch.setattr(conn, "_filesystem", lambda: FakeFS())
    assert conn._resolve_year_files("tas", "historical") == {}


def test_harmonization_identity():
    ds = xr.Dataset(
        {
            "tas": (("time", "lat", "lon"), np.full((2, 2, 2), 295.0)),
            "pr": (("time", "lat", "lon"), np.full((2, 2, 2), 3e-5)),
            "sfcWind": (("time", "lat", "lon"), np.full((2, 2, 2), 4.0)),
        },
        coords={"time": [0, 1], "lat": [10.0, 10.25], "lon": [200.0, 200.25]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(3e-5)
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(295.0)
    assert out[CanonicalVar.WIND_SPEED].attrs["units"] == "m s-1"
