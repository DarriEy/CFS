# SPDX-License-Identifier: MIT
"""CFSv2/CDAS offline tests: cycle/lead selection, .idx parsing, URL, mappings."""

from __future__ import annotations

from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.cfsv2 import (
    _MAPPINGS,
    _VARS,
    _file_url,
    _lead_available,
)
from cfs.connectors.protocols.grib_idx import cycle_for, parse_idx
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "cfsv2" in set(list_providers())


async def test_one_analysis_product():
    conn_cls = get_connector("cfsv2")
    async with conn_cls() as conn:
        ids = [p.id for p in await conn.list_products()]
    assert ids == ["cfsv2:cdas_flux"]


def test_cycle_floors_to_6h():
    # Most recent 00/06/12/18 UTC cycle at or before the start.
    assert cycle_for(datetime(2026, 6, 1, 1, 30), step_h=6) == datetime(2026, 6, 1, 0)
    assert cycle_for(datetime(2026, 6, 1, 7), step_h=6) == datetime(2026, 6, 1, 6)
    assert cycle_for(datetime(2026, 6, 1, 23), step_h=6) == datetime(2026, 6, 1, 18)


def test_lead_availability():
    # CDAS sfluxgrbf carries f00 (analysis) through f09.
    assert _lead_available(0) and _lead_available(9)
    assert not _lead_available(-1) and not _lead_available(10)


def test_file_url_structure():
    url = _file_url(datetime(2026, 6, 16, 0), 3)
    assert url.endswith("/cdas.20260616/cdas1.t00z.sfluxgrbf03.grib2")
    assert url.startswith("https://noaa-cfs-pds.s3.amazonaws.com/")


def test_parse_idx_byte_ranges():
    # Real CDAS sfluxgrbf .idx line shape: msg:start:date:VAR:level:fcst:
    idx = (
        "12:104857600:d=2026061600:TMP:2 m above ground:anl:\n"
        "13:105906176:d=2026061600:SPFH:2 m above ground:anl:\n"
        "30:209715200:d=2026061600:PRATE:surface:0-0 day ave fcst:\n"
    )
    recs = parse_idx(idx)
    assert recs[0] == ("TMP", "2 m above ground", 104857600)
    assert recs[1] == ("SPFH", "2 m above ground", 105906176)
    assert recs[2] == ("PRATE", "surface", 209715200)


def test_all_identity_si_components():
    # Every CFSv2 sfluxgrbf surface field is already canonical SI → identity.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in _MAPPINGS)
    canon = {v.canonical for v in _VARS}
    # All 8 forcing vars: vector winds (u/v), precip flux, both radiation comps.
    assert {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SPECIFIC_HUMIDITY,
            CanonicalVar.SURFACE_AIR_PRESSURE, CanonicalVar.EASTWARD_WIND,
            CanonicalVar.NORTHWARD_WIND, CanonicalVar.PRECIPITATION_FLUX,
            CanonicalVar.SHORTWAVE_RADIATION_DOWN,
            CanonicalVar.LONGWAVE_RADIATION_DOWN} == canon
