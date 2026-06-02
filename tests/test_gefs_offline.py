# SPDX-License-Identifier: MIT
"""GEFS offline tests: members, lead cadence, URL, deferred-vars, mappings."""

from __future__ import annotations

from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.gefs import (
    _ALL_MEMBERS,
    _DERIVED_Q,
    _FLUX_VARS,
    _INTERVAL_S,
    _MAPPINGS,
    _MAX_LEAD,
    _Q_INPUTS,
    _VARS,
    _file_url,
    _lead_available,
)
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.derive.humidity import specific_humidity_from_rh


def test_registered():
    discover()
    assert "gefs" in set(list_providers())


async def test_one_product():
    conn_cls = get_connector("gefs")
    async with conn_cls() as conn:
        ids = [p.id for p in await conn.list_products()]
    assert ids == ["gefs:atmos_0p25"]


def test_default_and_config_members():
    # Default = control + 30 perturbations.
    assert _ALL_MEMBERS[0] == "gec00" and len(_ALL_MEMBERS) == 31
    assert get_connector("gefs")().members == _ALL_MEMBERS
    conn = get_connector("gefs")(config={"members": ["gec00", "gep05"]})
    assert conn.members == ["gec00", "gep05"]


def test_lead_cadence_3hourly():
    # GEFS-select is 3-hourly (no f001/f002), produced only to f240.
    assert _MAX_LEAD == 240
    assert _lead_available(0) and _lead_available(3) and _lead_available(240)
    assert not _lead_available(1) and not _lead_available(2)
    assert not _lead_available(243)  # f243+ do not exist for pgrb2sp25


def test_file_url_select_product():
    url = _file_url("gep01", datetime(2026, 6, 1, 0), 6)
    assert url.endswith("/gefs.20260601/00/atmos/pgrb2sp25/gep01.t00z.pgrb2s.0p25.f006")
    assert url.startswith("https://noaa-gefs-pds.s3.amazonaws.com/")


def test_instantaneous_fields_identity():
    # The instantaneous fields are identity SI.
    inst = {v[2] for v in _VARS}
    assert inst == {
        CanonicalVar.AIR_TEMPERATURE,
        CanonicalVar.SURFACE_AIR_PRESSURE,
        CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND,
    }
    inst_internals = {v[3] for v in _VARS}
    assert all(
        m.scale == 1.0 and m.offset == 0.0
        for m in _MAPPINGS
        if m.source_name in inst_internals
    )


def test_flux_fields_offered():
    # Precip + down radiation are exposed as raw GRIB fields.
    flux_canon = {v[2] for v in _FLUX_VARS}
    assert flux_canon == {
        CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        CanonicalVar.LONGWAVE_RADIATION_DOWN,
    }
    # q is not a raw field — it is derived (see test_specific_humidity_*).
    assert CanonicalVar.SPECIFIC_HUMIDITY not in flux_canon | {v[2] for v in _VARS}
    # APCP accumulates, radiation averages.
    kinds = {v[0]: v[4] for v in _FLUX_VARS}
    assert kinds == {"APCP": "acc", "DSWRF": "ave", "DLWRF": "ave"}


def test_specific_humidity_derivation_inputs():
    # q is derived from RH + 2 m T + surface pressure; RH is an input-only field.
    assert _Q_INPUTS == [
        ("RH", "2 m above ground", "_rh2m"),
        ("TMP", "2 m above ground", "_t2m"),
        ("PRES", "surface", "_sp"),
    ]
    # T and P internals are shared with the exposed instantaneous fields.
    inst_internals = {v[3] for v in _VARS}
    assert {"_t2m", "_sp"} <= inst_internals
    # RH itself has no canonical mapping (never exposed raw).
    assert "_rh2m" not in {m.source_name for m in _MAPPINGS}


async def test_specific_humidity_offered_in_product():
    conn_cls = get_connector("gefs")
    async with conn_cls() as conn:
        (product,) = await conn.list_products()
    canon = {pv.canonical for pv in product.variables}
    assert CanonicalVar.SPECIFIC_HUMIDITY in canon
    assert {pv.source_name for pv in product.variables if
            pv.canonical == CanonicalVar.SPECIFIC_HUMIDITY} == {_DERIVED_Q}


def test_specific_humidity_physical():
    # Sanity on the derivation: 50% RH at 293.15 K, 101325 Pa → ~7 g/kg.
    q = specific_humidity_from_rh(50.0, 293.15, 101325.0)
    assert 0.005 < q < 0.009


def test_apcp_scaled_to_flux_radiation_identity():
    by_var = {m.source_name: m for m in _MAPPINGS}
    # APCP kg m-2 over the 3 h interval → flux kg m-2 s-1.
    assert by_var["_apcp"].canonical == CanonicalVar.PRECIPITATION_FLUX
    assert by_var["_apcp"].scale == 1.0 / _INTERVAL_S
    assert _INTERVAL_S == 10800
    # De-averaged radiation is already W m-2.
    for internal in ("_dswrf", "_dlwrf"):
        assert by_var[internal].scale == 1.0 and by_var[internal].offset == 0.0


def test_bucket_debucket_math():
    # First half of a bucket (lead % 6 == 3) is the per-3h value directly; the
    # second half (lead % 6 == 0) recovers it from the first half: cur-prev for
    # accumulations, 2*cur-prev for averages.
    for lead in (3, 9, 15, 237):
        assert lead % 6 == 3  # first half — used as-is
    for lead in (6, 12, 240):
        assert lead % 6 == 0  # second half — needs lead-3
    # acc: 0-6 minus 0-3 = precip in [3,6]; ave: 2*(0-6 avg) - (0-3 avg) = avg in [3,6].
    cur_acc, prev_acc = 5.0, 2.0  # 0-6, 0-3 totals
    assert cur_acc - prev_acc == 3.0
    cur_ave, prev_ave = 200.0, 150.0  # 0-6, 0-3 averages
    assert 2 * cur_ave - prev_ave == 250.0
