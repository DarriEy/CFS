# SPDX-License-Identifier: MIT
"""W5E5 offline tests: products, identity/scalar-wind mappings, chunk selection."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.w5e5 import (
    _MAPPINGS,
    W5E5Connector,
    _parse_chunk,
    _select_chunks,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar

# A realistic W5E5v2.0 directory listing (the real chunk boundaries).
_FILES = [
    "tas_W5E5v2.0_19790101-19801231.nc",
    "tas_W5E5v2.0_19810101-19901231.nc",
    "tas_W5E5v2.0_19910101-20001231.nc",
    "tas_W5E5v2.0_20010101-20101231.nc",
    "tas_W5E5v2.0_20110101-20191231.nc",
    "pr_W5E5v2.0_19790101-19801231.nc",
    "pr_W5E5v2.0_19810101-19901231.nc",
]


def test_registered():
    discover()
    assert "w5e5" in list_providers()


def test_one_product():
    ids = {p.id for p in asyncio.run(W5E5Connector().list_products())}
    assert ids == {"w5e5:obsclim_daily"}


def test_all_identity_and_scalar_wind():
    # Every W5E5 field is canonical SI → identity (no scale/offset/de-accum).
    assert all(m.scale == 1.0 and m.offset == 0.0 and not m.deaccumulate for m in _MAPPINGS)
    canon = {m.canonical for m in _MAPPINGS}
    # Wind is the SCALAR sfcWind → wind_speed; W5E5 ships no u/v components.
    assert CanonicalVar.WIND_SPEED in canon
    assert CanonicalVar.EASTWARD_WIND not in canon and CanonicalVar.NORTHWARD_WIND not in canon
    by_canon = {m.canonical: m.source_name for m in _MAPPINGS}
    assert by_canon[CanonicalVar.WIND_SPEED] == "sfcWind"


def test_parse_chunk():
    assert _parse_chunk("tas_W5E5v2.0_19790101-19801231.nc") == (
        "tas", datetime(1979, 1, 1), datetime(1980, 12, 31),
    )
    assert _parse_chunk("sfcWind_W5E5v2.0_20110101-20191231.nc")[0] == "sfcWind"
    assert _parse_chunk("not_a_w5e5_file.nc") is None


def test_select_chunks_overlap():
    # A 1980→1982 window must pull the 1979-1980 and 1981-1990 tas chunks (in order).
    sel = _select_chunks("tas", _FILES, datetime(1980, 6, 1), datetime(1982, 6, 1))
    assert sel == ["tas_W5E5v2.0_19790101-19801231.nc", "tas_W5E5v2.0_19810101-19901231.nc"]
    # A single-decade window inside one chunk pulls exactly that chunk.
    sel2 = _select_chunks("tas", _FILES, datetime(1995, 1, 1), datetime(1995, 2, 1))
    assert sel2 == ["tas_W5E5v2.0_19910101-20001231.nc"]
    # Only the requested variable's chunks are returned.
    assert all(f.startswith("pr_") for f in _select_chunks("pr", _FILES, datetime(1979, 1, 1), datetime(1979, 6, 1)))
