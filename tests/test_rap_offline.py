# SPDX-License-Identifier: MIT
"""RAP offline tests: registration, identity mappings, product, URL + lead structure."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.rap import (
    _FCST_FIELDS,
    _MAPPINGS,
    RAPConnector,
    _file_url,
    _max_lead,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "rap" in list_providers()


def test_one_product():
    ids = [p.id for p in asyncio.run(RAPConnector().list_products())]
    assert ids == ["rap:awp130_fcst"]


def test_all_identity_mappings():
    # Every RAP field is already canonical SI → identity.
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    canon = {f[2] for f in _FCST_FIELDS}
    # Includes instantaneous precip flux and both radiation components.
    assert {CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.SHORTWAVE_RADIATION_DOWN,
            CanonicalVar.LONGWAVE_RADIATION_DOWN, CanonicalVar.SPECIFIC_HUMIDITY} <= canon


def test_radiation_comes_from_bgrb():
    # Radiation is absent from the pgrb file; it must be sourced from bgrb.
    src = {f[2]: f[3] for f in _FCST_FIELDS}
    assert src[CanonicalVar.SHORTWAVE_RADIATION_DOWN] == "bgrb"
    assert src[CanonicalVar.LONGWAVE_RADIATION_DOWN] == "bgrb"
    assert src[CanonicalVar.AIR_TEMPERATURE] == "pgrb"
    assert src[CanonicalVar.PRECIPITATION_FLUX] == "pgrb"


def test_file_url_and_lead_structure():
    cyc = datetime(2026, 6, 13, 0)
    assert _file_url(cyc, 6, "pgrb").endswith("rap.20260613/rap.t00z.awp130pgrbf06.grib2")
    assert _file_url(cyc, 6, "bgrb").endswith("rap.20260613/rap.t00z.awp130bgrbf06.grib2")
    # Standard cycles to f21; the 03/09/15/21Z runs extend to f51.
    assert _max_lead(0) == 21
    assert _max_lead(12) == 21
    assert _max_lead(3) == 51
    assert _max_lead(21) == 51
