# SPDX-License-Identifier: MIT
"""Offline connector checks: catalog integrity + harmonization on synthetic cubes."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.aorc import _MAPPINGS as AORC_MAPPINGS
from cfs.connectors.chirps import _MAPPINGS as CHIRPS_MAPPINGS
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CANONICAL, CanonicalVar
from cfs.subset.canonical import harmonize


def test_all_providers_registered():
    discover()
    assert set(list_providers()) >= {"era5_arco", "aorc", "chirps"}


@pytest.mark.parametrize("slug", ["era5_arco", "aorc", "chirps"])
async def test_product_catalog_valid(slug):
    discover()
    async with get_connector(slug)() as conn:
        products = await conn.list_products()
    assert products
    for p in products:
        assert p.id.startswith(f"{slug}:")
        assert p.variables
        # Every advertised canonical var is a real vocabulary entry.
        for v in p.variables:
            assert v.canonical in CANONICAL


def _aorc_like():
    shape = (4, 3, 3)
    return xr.Dataset(
        {
            "TMP_2maboveground": (("time", "latitude", "longitude"), np.full(shape, 290.0)),
            "APCP_surface": (("time", "latitude", "longitude"), np.full(shape, 3.6)),  # kg/m2/hr
            "UGRD_10maboveground": (("time", "latitude", "longitude"), np.full(shape, 2.0)),
        },
        coords={"time": np.arange(4), "latitude": [50.0, 50.1, 50.2], "longitude": [250.0, 250.1, 250.2]},
    )


def test_aorc_precip_accumulation_to_flux():
    out = harmonize(_aorc_like(), AORC_MAPPINGS)
    # 3.6 kg/m2 over 1 h / 3600 s = 0.001 kg m-2 s-1
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(0.001, rel=1e-6)
    assert out[CanonicalVar.AIR_TEMPERATURE].attrs["units"] == "K"
    assert out[CanonicalVar.EASTWARD_WIND].attrs["units"] == "m s-1"


def test_chirps_daily_to_flux():
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), np.full((2, 2, 2), 8.64))},  # mm/day
        coords={"time": [0, 1], "latitude": [0.0, 0.05], "longitude": [30.0, 30.05]},
    )
    out = harmonize(ds, CHIRPS_MAPPINGS)
    # 8.64 mm/day / 86400 s = 1e-4 kg m-2 s-1
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(1e-4, rel=1e-6)
