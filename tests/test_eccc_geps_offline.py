# SPDX-License-Identifier: MIT
"""ECCC GEPS offline tests: products, mappings, URL pattern, lead grid, run regex."""

from __future__ import annotations

import asyncio
import re

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.eccc_geps import (
    _ACCUM_INTERNALS,
    _FIELDS,
    _MAPPINGS,
    ECCCGEPSConnector,
    _file_url,
    _lead_grid,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "eccc_geps" in list_providers()


def test_one_product():
    ids = {p.id for p in asyncio.run(ECCCGEPSConnector().list_products())}
    assert ids == {"eccc_geps:global_0p5_mean"}


def test_all_eight_forcing_vars_offered():
    canon = {canon for _t, canon, _i, _a in _FIELDS}
    assert canon == {
        CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SPECIFIC_HUMIDITY,
        CanonicalVar.SURFACE_AIR_PRESSURE, CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND, CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.LONGWAVE_RADIATION_DOWN,
    }


def test_accumulated_set_is_radiation_and_precip():
    # Radiation + precip are accumulated since run start (de-accumulated to flux
    # in-connector); the state fields are instantaneous identity.
    assert {"_dswrf", "_dlwrf", "_apcp"} == _ACCUM_INTERNALS
    # All harmonize mappings are identity (conversion happens before harmonize).
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)


def test_lead_grid_3h_then_6h():
    grid = _lead_grid()
    assert grid[0] == 0 and grid[1] == 3
    assert 192 in grid and 195 not in grid          # 3-hourly stops at 192
    assert 198 in grid and grid[-1] == 384          # 6-hourly thereafter
    # No gaps within each regime.
    early = [L for L in grid if L <= 192]
    assert early == list(range(0, 193, 3))


def test_file_url_structure():
    url = _file_url(0, 3, "2026061800", "TMP_TGL_2m")
    assert url.endswith(
        "/ensemble/geps/grib2/raw/00/003/"
        "CMC_geps-raw_TMP_TGL_2m_latlon0p5x0p5_2026061800_P003_allmbrs.grib2"
    )


def test_run_regex_extracts_init_datetime():
    fname = "CMC_geps-raw_TMP_TGL_2m_latlon0p5x0p5_2026061812_P000_allmbrs.grib2"
    m = re.search(r"_latlon0p5x0p5_(\d{10})_P000_", fname)
    assert m and m.group(1) == "2026061812" and int(m.group(1)[-2:]) == 12
