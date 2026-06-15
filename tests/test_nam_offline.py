# SPDX-License-Identifier: MIT
"""NAM offline tests: registration, product, mappings, lead/accumulation logic."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.nam import (
    _INST_FIELDS,
    _MAPPINGS,
    NAMConnector,
    _accum_ref,
    _file_url,
    _lead_available,
    _lead_step,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "nam" in list_providers()


def test_one_product():
    ids = [p.id for p in asyncio.run(NAMConnector().list_products())]
    assert ids == ["nam:awphys_fcst"]


def test_instantaneous_fields_identity():
    # Instantaneous fields are canonical SI → identity; precip handled separately.
    inst = {m.canonical for m in _MAPPINGS if m.canonical != CanonicalVar.PRECIPITATION_FLUX}
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    assert CanonicalVar.PRECIPITATION_FLUX not in {f[2] for f in _INST_FIELDS}
    assert CanonicalVar.PRECIPITATION_FLUX in {m.canonical for m in _MAPPINGS}
    assert {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SPECIFIC_HUMIDITY,
            CanonicalVar.SHORTWAVE_RADIATION_DOWN} <= inst


def test_lead_availability_hourly_then_3hourly():
    assert _lead_available(1) and _lead_available(36)          # hourly to f36
    assert _lead_available(39) and not _lead_available(37)     # 3-hourly after f36
    assert _lead_available(84) and not _lead_available(85)     # horizon
    assert not _lead_available(0)                              # no f00 forcing


def test_lead_step():
    assert _lead_step(1) == 1 and _lead_step(36) == 1
    assert _lead_step(39) == 3 and _lead_step(84) == 3


def test_accum_ref_resets_every_12h():
    # APCP accumulation reference = 12*floor((N-1)/12): 0 for f01-f12, 12 for f13-f24, ...
    assert _accum_ref(1) == 0 and _accum_ref(12) == 0
    assert _accum_ref(13) == 12 and _accum_ref(24) == 12
    assert _accum_ref(25) == 24 and _accum_ref(36) == 24
    assert _accum_ref(48) == 36


def test_deaccum_boundary_vs_subtraction_branch():
    # At a reset boundary, prev_lead == ref → inc is the run-total directly;
    # otherwise prev_lead is inside the same block → subtraction.
    for lead in (1, 13, 25):  # first lead after each reset
        assert lead - _lead_step(lead) == _accum_ref(lead)
    for lead in (6, 14, 24):  # mid-block
        assert lead - _lead_step(lead) != _accum_ref(lead)
        assert _accum_ref(lead - _lead_step(lead)) == _accum_ref(lead)  # same block


def test_file_url():
    assert _file_url(datetime(2026, 6, 13, 12), 13).endswith(
        "nam.20260613/nam.t12z.awphys13.tm00.grib2"
    )
