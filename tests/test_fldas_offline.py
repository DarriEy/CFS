# SPDX-License-Identifier: MIT
"""FLDAS offline tests: registration, URL building, products, mappings."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.fldas import _MAPPINGS as FLDAS_MAPPINGS
from cfs.connectors.fldas import _PRODUCTS, _opendap_url
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "fldas" in set(list_providers())


def test_fldas_url_structure_global_monthly():
    # Monthly global: /{year}/FLDAS_NOAH01_C_GL_M.A{YYYYMM}.001.nc
    url = _opendap_url("FLDAS_NOAH01_C_GL_M.001", "FLDAS_NOAH01_C_GL_M", 2020, 1)
    assert url.endswith(
        "/FLDAS_NOAH01_C_GL_M.001/2020/FLDAS_NOAH01_C_GL_M.A202001.001.nc"
    )


def test_fldas_url_zero_pads_month():
    url = _opendap_url("FLDAS_NOAH01_C_GL_M.001", "FLDAS_NOAH01_C_GL_M", 1999, 12)
    assert url.endswith("/1999/FLDAS_NOAH01_C_GL_M.A199912.001.nc")


async def test_fldas_lists_products():
    conn_cls = get_connector("fldas")
    async with conn_cls() as conn:
        ids = {p.id for p in await conn.list_products()}
    assert ids == {"fldas:noah_global_monthly"}


def test_fldas_product_keys_match_url_builder():
    # Every product key must resolve to a (collection, prefix, ...) tuple.
    assert set(_PRODUCTS) == {"noah_global_monthly"}
    for collection, prefix, *_ in _PRODUCTS.values():
        assert collection.startswith("FLDAS_NOAH01_")
        assert prefix.startswith("FLDAS_NOAH01_")


def test_fldas_all_identity_mappings():
    # Every FLDAS forcing field is already canonical SI → scale 1, offset 0.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in FLDAS_MAPPINGS)


def test_fldas_wind_is_scalar_speed():
    # FLDAS ships only a scalar wind (no u/v) → maps to wind_speed.
    winds = [m.canonical for m in FLDAS_MAPPINGS if "Wind" in m.source_name]
    assert winds == [CanonicalVar.WIND_SPEED]
    assert CanonicalVar.EASTWARD_WIND not in {m.canonical for m in FLDAS_MAPPINGS}


def test_fldas_harmonization_identity():
    # Synthetic FLDAS-shaped cube on the native X/Y grid.
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {
            "Tair_f_tavg": (("time", "Y", "X"), np.full(shape, 298.0)),
            "Rainf_f_tavg": (("time", "Y", "X"), np.full(shape, 2e-5)),
            "Wind_f_tavg": (("time", "Y", "X"), np.full(shape, 2.5)),
            "Psurf_f_tavg": (("time", "Y", "X"), np.full(shape, 90000.0)),
        },
        coords={"time": [0], "Y": [-2.0, -1.9], "X": [36.0, 36.1]},
    )
    out = harmonize(ds, FLDAS_MAPPINGS, lat_name="Y", lon_name="X")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(298.0)
    # Rainf_f_tavg is already a flux (kg m-2 s-1) — identity, no /3600 or /86400.
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(2e-5)
    assert float(out[CanonicalVar.WIND_SPEED].values.flat[0]) == pytest.approx(2.5)
    assert float(out[CanonicalVar.SURFACE_AIR_PRESSURE].values.flat[0]) == pytest.approx(90000.0)
    assert "latitude" in out.coords and "longitude" in out.coords
