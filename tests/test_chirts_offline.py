# SPDX-License-Identifier: MIT
"""CHIRTS offline tests: registration, URL builder, Tmax/Tmin -> mean K harmonize."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.chirts import (
    _MAPPINGS,
    _TMEAN,
    TMAX_FILE,
    TMIN_FILE,
    _year_url,
)
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "chirts" in list_providers()
    conn = get_connector("chirts")()
    assert conn.slug == "chirts"


def test_url_structure_is_per_year_per_variable():
    tmax = _year_url(TMAX_FILE, 2016)
    tmin = _year_url(TMIN_FILE, 2016)
    assert tmax.endswith("/Tmax/Tmax.2016.nc")
    assert tmin.endswith("/Tmin/Tmin.2016.nc")
    # base resolves to the global p05 CHIRTS daily archive
    assert "CHIRTSdaily/v1.0/global_netcdf_p05" in tmax


async def test_product_catalog():
    products = await get_connector("chirts")().list_products()
    assert len(products) == 1
    p = products[0]
    assert p.id == "chirts:daily"
    assert [v.canonical for v in p.variables] == [CanonicalVar.AIR_TEMPERATURE]


def _mean_dataset(tmax_c, tmin_c):
    """Build the connector's internal mean field (°C) from synthetic Tmax/Tmin."""
    coords = {"time": [0], "latitude": [-1.0, -0.95], "longitude": [36.0, 36.05]}
    tmax = xr.DataArray(np.full((1, 2, 2), tmax_c), dims=("time", "latitude", "longitude"), coords=coords)
    tmin = xr.DataArray(np.full((1, 2, 2), tmin_c), dims=("time", "latitude", "longitude"), coords=coords)
    return xr.Dataset({_TMEAN: (tmax + tmin) / 2.0})


def test_mean_celsius_to_kelvin():
    # Tmax 30°C, Tmin 20°C -> mean 25°C -> 298.15 K.
    ds = _mean_dataset(30.0, 20.0)
    out = harmonize(ds, _MAPPINGS)
    val = float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0])
    assert val == pytest.approx(298.15, abs=1e-6)
    assert out[CanonicalVar.AIR_TEMPERATURE].attrs.get("units") == "K"


def test_mean_freezing_point():
    # Tmax 5°C, Tmin -5°C -> mean 0°C -> 273.15 K.
    ds = _mean_dataset(5.0, -5.0)
    out = harmonize(ds, _MAPPINGS)
    val = float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0])
    assert val == pytest.approx(273.15, abs=1e-6)


def test_harmonize_only_requested():
    ds = _mean_dataset(30.0, 20.0)
    out = harmonize(ds, _MAPPINGS, requested=[CanonicalVar.AIR_TEMPERATURE])
    assert list(out.data_vars) == [CanonicalVar.AIR_TEMPERATURE]
