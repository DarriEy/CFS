# SPDX-License-Identifier: MIT
"""AgERA5 offline tests: products, mappings, day-grouping, partial coverage."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.agera5 import _MAPPINGS, _VARS, AgERA5Connector, _days
from cfs.core.models import TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "agera5" in list_providers()


def test_one_daily_product():
    ids = {p.id for p in asyncio.run(AgERA5Connector().list_products())}
    assert ids == {"agera5:daily"}


def test_partial_forcing_coverage():
    # AgERA5 is agromet: only this subset of canonical vars, no longwave/pressure/q.
    canon = {v.canonical for v in _VARS}
    assert canon == {
        CanonicalVar.AIR_TEMPERATURE, CanonicalVar.DEWPOINT_TEMPERATURE,
        CanonicalVar.WIND_SPEED, CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN,
    }
    assert CanonicalVar.LONGWAVE_RADIATION_DOWN not in canon
    assert CanonicalVar.SURFACE_AIR_PRESSURE not in canon


def test_unit_conversions():
    by_canon = {m.canonical: m for m in _MAPPINGS}
    # Precip mm/day and radiation J/m2/day both divide by 86400 s.
    assert by_canon[CanonicalVar.PRECIPITATION_FLUX].scale == pytest.approx(1.0 / 86400.0)
    assert by_canon[CanonicalVar.SHORTWAVE_RADIATION_DOWN].scale == pytest.approx(1.0 / 86400.0)
    # Temperature and wind are identity (already K / m s-1).
    assert by_canon[CanonicalVar.AIR_TEMPERATURE].scale == 1.0
    assert by_canon[CanonicalVar.WIND_SPEED].scale == 1.0


def test_day_grouping_spans_months():
    groups = _days(TimeRange(start=datetime(2020, 1, 30), end=datetime(2020, 2, 2)))
    assert groups == [(2020, 1, ["30", "31"]), (2020, 2, ["01", "02"])]


def test_day_grouping_single_month():
    groups = _days(TimeRange(start=datetime(2021, 6, 5), end=datetime(2021, 6, 7)))
    assert groups == [(2021, 6, ["05", "06", "07"])]
