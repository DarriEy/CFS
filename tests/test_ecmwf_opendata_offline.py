# SPDX-License-Identifier: MIT
"""ECMWF open-data offline tests: product, mappings, step/URL logic, index parse."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.ecmwf_opendata import (
    _ACCUM_FIELDS,
    _INST_FIELDS,
    _MAPPINGS,
    ECMWFOpenDataConnector,
    _file_url,
    _index_url,
    _max_step,
    _prev_step,
    _step_available,
)
from cfs.connectors.protocols.grib_idx import parse_ecmwf_index
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "ecmwf_opendata" in list_providers()


def test_products_ifs_aifs_ens():
    ids = {p.id for p in asyncio.run(ECMWFOpenDataConnector().list_products())}
    assert ids == {"ecmwf_opendata:ifs_0p25", "ecmwf_opendata:aifs_0p25",
                   "ecmwf_opendata:enfo_0p25"}


def test_ens_default_members_and_config():
    assert len(ECMWFOpenDataConnector().members) == 50            # all perturbed members
    conn = ECMWFOpenDataConnector(config={"members": [1, 2, 3]})
    assert conn.members == ["1", "2", "3"]                        # coerced to str


def test_mappings_cover_era5_like_set():
    canon = {m.canonical for m in _MAPPINGS}
    assert {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.DEWPOINT_TEMPERATURE,
            CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND,
            CanonicalVar.SURFACE_AIR_PRESSURE, CanonicalVar.PRECIPITATION_FLUX,
            CanonicalVar.SHORTWAVE_RADIATION_DOWN,
            CanonicalVar.LONGWAVE_RADIATION_DOWN} == canon
    # tp/ssrd/strd are the accumulated (de-accumulated) fields; the rest instantaneous.
    assert {p for p, _c, _s in _ACCUM_FIELDS} == {"tp", "ssrd", "strd"}
    assert CanonicalVar.PRECIPITATION_FLUX not in {c for _p, c in _INST_FIELDS}


def test_step_horizon_and_spacing_ifs():
    # IFS: 360 h for 00/12Z, 90 h short_max for 06/18Z; 3-hourly to 144 then 6-hourly.
    assert _max_step(0, 90) == 360 and _max_step(12, 90) == 360
    assert _max_step(6, 90) == 90 and _max_step(18, 90) == 90
    assert _step_available(3, 0, "ifs", 90) and _step_available(144, 0, "ifs", 90)
    assert not _step_available(147, 0, "ifs", 90) and _step_available(150, 0, "ifs", 90)
    assert not _step_available(93, 6, "ifs", 90)        # past the 90h horizon
    assert _step_available(0, 0, "ifs", 90)             # analysis step


def test_step_horizon_and_spacing_aifs():
    # AIFS: 6-hourly to 360 h, all cycles (short_max 360).
    assert _max_step(0, 360) == 360 and _max_step(6, 360) == 360
    assert _step_available(6, 0, "aifs", 360) and _step_available(360, 6, "aifs", 360)
    assert not _step_available(3, 0, "aifs", 360)       # no 3-hourly steps
    assert not _step_available(366, 0, "aifs", 360)     # past horizon


def test_step_horizon_ens():
    # ENS: 360 h for 00/12Z, 144 h short_max for 06/18Z (longer than IFS's 90 h).
    assert _max_step(0, 144) == 360 and _max_step(6, 144) == 144
    assert _step_available(144, 6, "ifs", 144) and not _step_available(150, 6, "ifs", 144)


def test_prev_step_spacing():
    assert _prev_step(3, "ifs") == 0 and _prev_step(6, "ifs") == 3       # 3h spacing early
    assert _prev_step(144, "ifs") == 141
    assert _prev_step(150, "ifs") == 144 and _prev_step(156, "ifs") == 150  # 6h spacing late
    assert _prev_step(6, "aifs") == 0 and _prev_step(120, "aifs") == 114    # always 6h


def test_urls():
    cyc = datetime(2026, 6, 15, 0)
    assert _file_url("ifs", "oper", "oper-fc", cyc, 3).endswith(
        "20260615/00z/ifs/0p25/oper/20260615000000-3h-oper-fc.grib2"
    )
    assert _file_url("aifs-single", "oper", "oper-fc", cyc, 6).endswith(
        "20260615/00z/aifs-single/0p25/oper/20260615000000-6h-oper-fc.grib2"
    )
    assert _file_url("ifs", "enfo", "enfo-ef", cyc, 3).endswith(
        "20260615/00z/ifs/0p25/enfo/20260615000000-3h-enfo-ef.grib2"
    )
    # ECMWF index REPLACES .grib2 (not appended like NOAA .idx).
    assert _index_url("ifs", "enfo", "enfo-ef", cyc, 3).endswith("-3h-enfo-ef.index")
    assert ".grib2" not in _index_url("ifs", "oper", "oper-fc", cyc, 3)


def test_parse_ecmwf_index():
    # oper records carry no member (number=None); enfo records carry "number".
    text = (
        '{"param":"tp","levtype":"sfc","step":"3","_offset":0,"_length":691163}\n'
        '\n'
        '{"param":"2t","levtype":"sfc","step":"3","number":"5","_offset":700,"_length":800}\n'
    )
    recs = parse_ecmwf_index(text)
    assert recs == [
        ("tp", "sfc", "3", None, 0, 691163),
        ("2t", "sfc", "3", "5", 700, 800),
    ]
