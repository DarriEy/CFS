# SPDX-License-Identifier: MIT
"""GFS offline tests: cycle/lead selection, .idx parsing, URL, mappings."""

from __future__ import annotations

from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.gfs import (
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
    assert "gfs" in set(list_providers())


async def test_one_forecast_product():
    conn_cls = get_connector("gfs")
    async with conn_cls() as conn:
        ids = [p.id for p in await conn.list_products()]
    assert ids == ["gfs:forecast_0p25"]


def test_cycle_floors_to_6h():
    # Most recent 00/06/12/18 UTC cycle at or before the start.
    assert cycle_for(datetime(2026, 6, 1, 1, 30)) == datetime(2026, 6, 1, 0)
    assert cycle_for(datetime(2026, 6, 1, 7)) == datetime(2026, 6, 1, 6)
    assert cycle_for(datetime(2026, 6, 1, 23)) == datetime(2026, 6, 1, 18)


def test_lead_availability():
    assert _lead_available(0) and _lead_available(120)      # hourly to f120
    assert _lead_available(123) and not _lead_available(121)  # 3-hourly after f120
    assert not _lead_available(-1) and not _lead_available(400)  # out of range


def test_file_url_structure():
    url = _file_url(datetime(2026, 6, 1, 0), 3)
    assert url.endswith("/gfs.20260601/00/atmos/gfs.t00z.pgrb2.0p25.f003")
    assert url.startswith("https://noaa-gfs-bdp-pds.s3.amazonaws.com/")


def test_parse_idx_byte_ranges():
    # Real .idx line shape: msg:start:date:VAR:level:fcst:
    idx = (
        "580:415216076:d=2026060100:TMP:2 m above ground:anl:\n"
        "581:415724904:d=2026060100:SPFH:2 m above ground:anl:\n"
        "589:421301786:d=2026060100:PRATE:surface:anl:\n"
    )
    recs = parse_idx(idx)
    assert recs[0] == ("TMP", "2 m above ground", 415216076)
    assert recs[1] == ("SPFH", "2 m above ground", 415724904)
    # the connector computes a message's end as (next start - 1)
    assert recs[2][2] == 421301786


def test_all_identity_si_components():
    # Every GFS surface field is already canonical SI → identity.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in _MAPPINGS)
    canon = {v.canonical for v in _VARS}
    # vector winds (u/v), precip as a flux, both radiation components.
    assert {CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND,
            CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.SHORTWAVE_RADIATION_DOWN,
            CanonicalVar.LONGWAVE_RADIATION_DOWN} <= canon
