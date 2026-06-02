# SPDX-License-Identifier: MIT
"""GLDAS offline tests: registration, URL building, products, mappings."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.gldas import _MAPPINGS as GLDAS_MAPPINGS
from cfs.connectors.gldas import _PRODUCTS, _opendap_url
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "gldas" in set(list_providers())


def test_gldas_url_structure_v21():
    # 2015-06-01 is day-of-year 152; GLDAS-2.1 uses version token 021.
    url = _opendap_url("GLDAS_NOAH025_3H.2.1", "021", 2015, 152, "20150601", "0300")
    assert url.endswith(
        "/GLDAS_NOAH025_3H.2.1/2015/152/GLDAS_NOAH025_3H.A20150601.0300.021.nc4"
    )


def test_gldas_url_structure_v20():
    # GLDAS-2.0 uses version token 020; 1985-01-01 is day-of-year 001.
    url = _opendap_url("GLDAS_NOAH025_3H.2.0", "020", 1985, 1, "19850101", "0000")
    assert url.endswith(
        "/GLDAS_NOAH025_3H.2.0/1985/001/GLDAS_NOAH025_3H.A19850101.0000.020.nc4"
    )


async def test_gldas_lists_both_collections():
    conn_cls = get_connector("gldas")
    async with conn_cls() as conn:
        ids = {p.id for p in await conn.list_products()}
    assert ids == {"gldas:noah025_3h", "gldas:noah025_3h_v20"}


def test_gldas_product_keys_match_url_builder():
    # Every product key must resolve to a (collection, version, ...) tuple.
    assert set(_PRODUCTS) == {"noah025_3h", "noah025_3h_v20"}
    for collection, version, *_ in _PRODUCTS.values():
        assert collection.startswith("GLDAS_NOAH025_3H.2.")
        assert version in {"020", "021"}


def test_gldas_all_identity_mappings():
    # Every GLDAS forcing field is already canonical SI → scale 1, offset 0.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in GLDAS_MAPPINGS)


def test_gldas_wind_is_scalar_speed():
    # GLDAS ships only a scalar wind (no u/v) → maps to wind_speed.
    winds = [m.canonical for m in GLDAS_MAPPINGS if "Wind" in m.source_name]
    assert winds == [CanonicalVar.WIND_SPEED]
    assert CanonicalVar.EASTWARD_WIND not in {m.canonical for m in GLDAS_MAPPINGS}


def test_gldas_harmonization_identity():
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {
            "Tair_f_inst": (("time", "lat", "lon"), np.full(shape, 290.0)),
            "Rainf_f_tavg": (("time", "lat", "lon"), np.full(shape, 1e-5)),
            "Wind_f_inst": (("time", "lat", "lon"), np.full(shape, 3.0)),
        },
        coords={"time": [0], "lat": [40.0, 40.25], "lon": [-100.0, -99.75]},
    )
    out = harmonize(ds, GLDAS_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(290.0)
    # Rainf_f_tavg is already a flux (kg m-2 s-1) — identity, no /3600 or /86400.
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-5)
    assert float(out[CanonicalVar.WIND_SPEED].values.flat[0]) == pytest.approx(3.0)
    assert "latitude" in out.coords and "longitude" in out.coords
