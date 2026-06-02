# SPDX-License-Identifier: MIT
"""NA-CORDEX offline tests: products-per-experiment, config knobs, mappings."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.na_cordex import _MAPPINGS, NACORDEXConnector
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "na_cordex" in list_providers()


async def test_one_product_per_experiment():
    conn = NACORDEXConnector()
    products = await conn.list_products()
    ids = {p.id for p in products}
    # NA-CORDEX combines historical+scenario (confirmed against the bucket listing).
    assert ids == {"na_cordex:eval", "na_cordex:hist-rcp45", "na_cordex:hist-rcp85"}


def test_defaults_and_config_knobs():
    assert NACORDEXConnector().grid == "NAM-22i"
    assert NACORDEXConnector().bias_correction == "raw"
    c = NACORDEXConnector(config={"grid": "NAM-44i", "bias_correction": "mbcn-gridMET"})
    assert c.grid == "NAM-44i"
    assert c.bias_correction == "mbcn-gridMET"


def test_all_identity_cf_si():
    # NA-CORDEX Zarrs are native CF/SI — every mapping is a pass-through.
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    canon = {m.canonical for m in _MAPPINGS}
    assert {CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.AIR_TEMPERATURE,
            CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.EASTWARD_WIND} <= canon
    # hurs is relative humidity, not specific humidity, and NA-CORDEX has no
    # surface pressure to derive q — so specific_humidity is deliberately not offered.
    assert CanonicalVar.SPECIFIC_HUMIDITY not in canon


def test_harmonization_identity():
    ds = xr.Dataset(
        {
            "tas": (("time", "lat", "lon"), np.full((2, 2, 2), 295.0)),
            "pr": (("time", "lat", "lon"), np.full((2, 2, 2), 3e-5)),
            "rsds": (("time", "lat", "lon"), np.full((2, 2, 2), 250.0)),
        },
        coords={"time": [0, 1], "lat": [10.0, 10.22], "lon": [-110.0, -109.78]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(3e-5)
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(295.0)
    assert out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].attrs["units"] == "W m-2"
