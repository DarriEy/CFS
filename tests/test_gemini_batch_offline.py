# SPDX-License-Identifier: MIT
"""Offline checks for the aorc_nwm + era5_cds connectors (salvaged batch)."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.aorc_nwm import _MAPPINGS as AORC_NWM_MAPPINGS
from cfs.connectors.aorc_nwm import _STORES
from cfs.connectors.era5_cds import _MAPPINGS as ERA5_CDS_MAPPINGS
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert {"aorc_nwm", "era5_cds"} <= set(list_providers())


# ── aorc_nwm ─────────────────────────────────────────────────────────

async def test_aorc_nwm_catalog():
    conn_cls = get_connector("aorc_nwm")
    async with conn_cls() as conn:
        products = await conn.list_products()
    assert [p.id for p in products] == ["aorc_nwm:conus_1km"]


def test_aorc_nwm_identity_and_stores():
    # NWM forcing is native SI → identity; every canonical var has a Zarr store.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in AORC_NWM_MAPPINGS)
    assert {m.canonical for m in AORC_NWM_MAPPINGS} == set(_STORES)
    assert _STORES[CanonicalVar.PRECIPITATION_FLUX] == "precip.zarr"


# ── era5_cds ─────────────────────────────────────────────────────────

async def test_era5_cds_catalog():
    conn_cls = get_connector("era5_cds")
    async with conn_cls() as conn:
        products = await conn.list_products()
    assert [p.id for p in products] == ["era5_cds:single_levels"]


def test_era5_cds_precip_scale_includes_water_density():
    # ERA5 tp is metres of water / hour -> kg m-2 s-1 needs *1000/3600 (not /3600).
    tp = next(m for m in ERA5_CDS_MAPPINGS if m.canonical == CanonicalVar.PRECIPITATION_FLUX)
    assert tp.scale == pytest.approx(1000.0 / 3600.0)
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {"tp": (("time", "latitude", "longitude"), np.full(shape, 0.0036))},  # m over 1 h
        coords={"time": [0], "latitude": [50.0, 50.25], "longitude": [5.0, 5.25]},
    )
    out = harmonize(ds, ERA5_CDS_MAPPINGS, lat_name="latitude", lon_name="longitude")
    # 0.0036 m/h -> 3.6 kg m-2 over 3600 s -> 1e-3 kg m-2 s-1
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-3, rel=1e-6)


def test_era5_cds_radiation_identity_per_hour():
    # ssrd/strd are J m-2 over 1 h -> W m-2 via /3600.
    ssrd = next(m for m in ERA5_CDS_MAPPINGS if m.canonical == CanonicalVar.SHORTWAVE_RADIATION_DOWN)
    assert ssrd.scale == pytest.approx(1.0 / 3600.0)
