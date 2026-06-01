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


def test_year_file_resolution_prefers_v2():
    # The dir-listing resolver must pick the _v2.0 correction over the base file.

    from cfs.connectors.nex_gddp import _YEAR_RE

    files = [
        "b/tas/tas_day_ACCESS-CM2_historical_r1i1p1f1_gn_2014.nc",
        "b/tas/tas_day_ACCESS-CM2_historical_r1i1p1f1_gn_2014_v2.0.nc",
        "b/tas/tas_day_ACCESS-CM2_historical_r1i1p1f1_gn_2013.nc",
    ]
    out = {}
    for path in files:
        m = _YEAR_RE.search(path.split("/")[-1])
        year, is_v2 = int(m.group(1)), "_v" in path
        if year not in out or is_v2:
            out[year] = path
    assert out[2014].endswith("_v2.0.nc")
    assert out[2013].endswith("gn_2013.nc")


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
