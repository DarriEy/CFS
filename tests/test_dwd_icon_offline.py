# SPDX-License-Identifier: MIT
"""DWD ICON-EU offline tests: products, mappings, URL/run-stamp parsing."""

from __future__ import annotations

import asyncio
import re

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.dwd_icon import (
    _MAPPINGS,
    _OFFERED,
    DWDICONConnector,
    _file_url,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "dwd_icon" in list_providers()


def test_one_eu_product():
    ids = {p.id for p in asyncio.run(DWDICONConnector().list_products())}
    assert ids == {"dwd_icon:eu_regular"}


def test_offers_six_of_eight_no_longwave_or_q():
    assert {
        CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SURFACE_AIR_PRESSURE,
        CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND,
        CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.SHORTWAVE_RADIATION_DOWN,
    } == _OFFERED
    assert CanonicalVar.LONGWAVE_RADIATION_DOWN not in _OFFERED
    assert CanonicalVar.SPECIFIC_HUMIDITY not in _OFFERED


def test_instant_identity_vs_accumulated_precip():
    by_canon = {m.canonical: m for m in _MAPPINGS}
    # Temperature/pressure/wind are identity SI.
    for canon in (CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SURFACE_AIR_PRESSURE,
                  CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND):
        assert by_canon[canon].scale == 1.0 and not by_canon[canon].deaccumulate
    # Precip is accumulated → de-accumulate + /3600.
    pr = by_canon[CanonicalVar.PRECIPITATION_FLUX]
    assert pr.deaccumulate and pr.scale == pytest.approx(1.0 / 3600.0)
    # Shortwave de-averaging is done in-connector → identity mapping.
    assert by_canon[CanonicalVar.SHORTWAVE_RADIATION_DOWN].scale == 1.0


def test_file_url_structure():
    url = _file_url(0, 2, "2026061800", "t_2m", "T_2M")
    assert url.endswith(
        "/00/t_2m/icon-eu_europe_regular-lat-lon_single-level_2026061800_002_T_2M.grib2.bz2"
    )
    assert url.startswith("https://opendata.dwd.de/weather/nwp/icon-eu/grib/")


def test_run_stamp_regex():
    # The run-discovery regex extracts the 10-digit YYYYMMDDHH stamp + cycle hour.
    fname = "icon-eu_europe_regular-lat-lon_single-level_2026061800_000_T_2M.grib2.bz2"
    m = re.search(r"_(\d{10})_\d{3}_T_2M\.grib2\.bz2", fname)
    assert m and m.group(1) == "2026061800" and int(m.group(1)[8:10]) == 0
