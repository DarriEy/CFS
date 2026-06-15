# SPDX-License-Identifier: MIT
"""NAM offline tests: products, mappings, lead/accumulation logic, URL patterns."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.nam import (
    _INST_FIELDS,
    _MAPPINGS_APCP,
    _MAPPINGS_PRATE,
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


def test_products_awphys_and_conusnest():
    ids = {p.id for p in asyncio.run(NAMConnector().list_products())}
    assert ids == {"nam:awphys_fcst", "nam:conusnest_fcst"}


def test_mappings_identity_both_products():
    # Instantaneous fields are canonical SI → identity; precip handled separately.
    for mappings in (_MAPPINGS_APCP, _MAPPINGS_PRATE):
        assert all(m.scale == 1.0 and not m.deaccumulate for m in mappings)
        assert CanonicalVar.PRECIPITATION_FLUX in {m.canonical for m in mappings}
    # awphys precip comes from APCP (de-accumulated); conusnest from PRATE (direct).
    assert "APCP" in {m.source_name for m in _MAPPINGS_APCP}
    assert "PRATE" in {m.source_name for m in _MAPPINGS_PRATE}
    assert CanonicalVar.PRECIPITATION_FLUX not in {f[2] for f in _INST_FIELDS}


def test_lead_availability():
    # awphys: hourly to f36, then 3-hourly to f84.
    assert _lead_available(1, 84, 36) and _lead_available(36, 84, 36)
    assert _lead_available(39, 84, 36) and not _lead_available(37, 84, 36)
    assert _lead_available(84, 84, 36) and not _lead_available(85, 84, 36)
    assert not _lead_available(0, 84, 36)                  # no f00 forcing
    # conusnest: hourly to f60.
    assert _lead_available(1, 60, 60) and _lead_available(60, 60, 60)
    assert not _lead_available(61, 60, 60)


def test_lead_step():
    assert _lead_step(1, 36) == 1 and _lead_step(36, 36) == 1
    assert _lead_step(39, 36) == 3 and _lead_step(84, 36) == 3
    assert _lead_step(60, 60) == 1                          # conusnest is all hourly


def test_accum_ref_resets_every_12h():
    # APCP accumulation reference = 12*floor((N-1)/12): 0 for f01-f12, 12 for f13-f24, ...
    assert _accum_ref(1) == 0 and _accum_ref(12) == 0
    assert _accum_ref(13) == 12 and _accum_ref(24) == 12
    assert _accum_ref(25) == 24 and _accum_ref(36) == 24
    assert _accum_ref(48) == 36


def test_deaccum_boundary_vs_subtraction_branch():
    # At a reset boundary, prev_lead == ref → inc is the run-total directly;
    # otherwise prev_lead is inside the same block → subtraction. (awphys spacing.)
    for lead in (1, 13, 25):  # first lead after each reset
        assert lead - _lead_step(lead, 36) == _accum_ref(lead)
    for lead in (6, 14, 24):  # mid-block
        assert lead - _lead_step(lead, 36) != _accum_ref(lead)
        assert _accum_ref(lead - _lead_step(lead, 36)) == _accum_ref(lead)  # same block


def test_file_urls():
    cyc = datetime(2026, 6, 13, 12)
    assert _file_url("awphys", cyc, 13).endswith("nam.20260613/nam.t12z.awphys13.tm00.grib2")
    assert _file_url("conusnest", cyc, 6).endswith(
        "nam.20260613/nam.t12z.conusnest.hiresf06.tm00.grib2"
    )
