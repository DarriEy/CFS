# SPDX-License-Identifier: MIT
"""Livneh offline tests: registration, products, mappings, URL, var resolution."""

from __future__ import annotations

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.livneh import (
    _CANON_TO_SOURCES,
    _MAPPINGS,
    _file_url,
    _years,
)
from cfs.core.models import TimeRange
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "livneh" in set(list_providers())


async def test_one_daily_product():
    conn_cls = get_connector("livneh")
    async with conn_cls() as conn:
        ids = [p.id for p in await conn.list_products()]
    assert ids == ["livneh:daily"]


def test_offers_four_partial_fields():
    # Livneh has only 4/8 forcing fields: precip, air temp (derived), scalar wind.
    canon = {m.canonical for m in _MAPPINGS}
    assert canon == {
        CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.AIR_TEMPERATURE,
        CanonicalVar.WIND_SPEED,
    }
    # No radiation / humidity / pressure.
    assert CanonicalVar.SHORTWAVE_RADIATION_DOWN not in canon
    assert CanonicalVar.SPECIFIC_HUMIDITY not in canon
    assert CanonicalVar.SURFACE_AIR_PRESSURE not in canon


def test_mappings_units():
    by_canon = {m.canonical: m for m in _MAPPINGS}
    # precip mm/day -> kg m-2 s-1
    assert by_canon[CanonicalVar.PRECIPITATION_FLUX].source_name == "prec"
    assert by_canon[CanonicalVar.PRECIPITATION_FLUX].scale == pytest.approx(1.0 / 86400.0)
    # air temperature is the derived daily-mean "tair", degC -> K
    assert by_canon[CanonicalVar.AIR_TEMPERATURE].source_name == "tair"
    assert by_canon[CanonicalVar.AIR_TEMPERATURE].offset == pytest.approx(273.15)
    # wind is scalar identity (m/s)
    assert by_canon[CanonicalVar.WIND_SPEED].source_name == "wind"
    assert by_canon[CanonicalVar.WIND_SPEED].scale == 1.0


def test_air_temperature_needs_both_tmax_and_tmin():
    assert _CANON_TO_SOURCES[CanonicalVar.AIR_TEMPERATURE] == ("tmax", "tmin")
    assert _CANON_TO_SOURCES[CanonicalVar.PRECIPITATION_FLUX] == ("prec",)
    assert _CANON_TO_SOURCES[CanonicalVar.WIND_SPEED] == ("wind",)


def test_file_url_structure():
    url = _file_url("prec", 2011)
    assert url.endswith("/Datasets/livneh/metvars/prec.2011.nc")
    assert url.startswith("https://psl.noaa.gov/thredds/dodsC/")


def test_years_span_inclusive():
    tr = TimeRange(start="2010-12-30T00:00:00", end="2012-01-02T00:00:00")
    assert _years(tr) == [2010, 2011, 2012]
