# SPDX-License-Identifier: MIT
"""GEFS offline tests: members, lead cadence, URL, deferred-vars, mappings."""

from __future__ import annotations

from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.gefs import _ALL_MEMBERS, _MAPPINGS, _VARS, _file_url, _lead_available
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar


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
    # GEFS-select is 3-hourly (no f001/f002).
    assert _lead_available(0) and _lead_available(3) and _lead_available(384)
    assert not _lead_available(1) and not _lead_available(2)
    assert not _lead_available(387)


def test_file_url_select_product():
    url = _file_url("gep01", datetime(2026, 6, 1, 0), 6)
    assert url.endswith("/gefs.20260601/00/atmos/pgrb2sp25/gep01.t00z.pgrb2s.0p25.f006")
    assert url.startswith("https://noaa-gefs-pds.s3.amazonaws.com/")


def test_instantaneous_fields_only():
    # v1 offers only the clean instantaneous fields; precip/radiation/humidity
    # are deferred (6-hour-bucket accumulation; RH not q).
    canon = {v[2] for v in _VARS}
    assert canon == {
        CanonicalVar.AIR_TEMPERATURE,
        CanonicalVar.SURFACE_AIR_PRESSURE,
        CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND,
    }
    assert CanonicalVar.PRECIPITATION_FLUX not in canon
    assert CanonicalVar.SHORTWAVE_RADIATION_DOWN not in canon
    assert CanonicalVar.SPECIFIC_HUMIDITY not in canon
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in _MAPPINGS)
