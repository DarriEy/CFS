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


def test_one_product():
    ids = [p.id for p in asyncio.run(ECMWFOpenDataConnector().list_products())]
    assert ids == ["ecmwf_opendata:ifs_0p25"]


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


def test_step_horizon_and_spacing():
    assert _max_step(0) == 360 and _max_step(12) == 360     # long runs
    assert _max_step(6) == 90 and _max_step(18) == 90       # short runs
    assert _step_available(3, 0) and _step_available(144, 0)        # 3-hourly to 144
    assert not _step_available(147, 0) and _step_available(150, 0)  # 6-hourly after
    assert not _step_available(93, 6)                              # past the 90h horizon
    assert _step_available(0, 0)                                   # analysis step


def test_prev_step_spacing():
    assert _prev_step(3) == 0 and _prev_step(6) == 3       # 3h spacing early
    assert _prev_step(144) == 141
    assert _prev_step(150) == 144 and _prev_step(156) == 150  # 6h spacing late


def test_urls():
    cyc = datetime(2026, 6, 15, 0)
    assert _file_url(cyc, 3).endswith(
        "20260615/00z/ifs/0p25/oper/20260615000000-3h-oper-fc.grib2"
    )
    # ECMWF index REPLACES .grib2 (not appended like NOAA .idx).
    assert _index_url(cyc, 3).endswith("20260615000000-3h-oper-fc.index")
    assert ".grib2" not in _index_url(cyc, 3)


def test_parse_ecmwf_index():
    text = (
        '{"param":"tp","levtype":"sfc","step":"3","_offset":0,"_length":691163}\n'
        '\n'
        '{"param":"2t","levtype":"sfc","step":"3","_offset":691163,"_length":700000}\n'
    )
    recs = parse_ecmwf_index(text)
    assert recs == [
        ("tp", "sfc", "3", 0, 691163),
        ("2t", "sfc", "3", 691163, 700000),
    ]
