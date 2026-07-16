# SPDX-License-Identifier: MIT
from datetime import datetime

import pytest

pytest.importorskip("xarray")

from cfs.connectors.cfsr import _MAPPINGS, CFSRConnector, _monthly_url, _months
from cfs.core.models import TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered_and_product_is_historical_companion():
    discover()
    assert "cfsr" in list_providers()


async def test_product_contract():
    product = (await CFSRConnector().list_products())[0]
    assert product.id == "cfsr:hourly_timeseries"
    assert product.temporal.start.year == 1979
    assert product.temporal.end.year == 2010
    assert {m.canonical for m in _MAPPINGS} == {
        CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SPECIFIC_HUMIDITY,
        CanonicalVar.SURFACE_AIR_PRESSURE, CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND, CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.LONGWAVE_RADIATION_DOWN,
    }


def test_monthly_paths_and_range():
    assert _monthly_url("tmp2m", 1979, 1).endswith("/1979/tmp2m.gdas.197901.grb2")
    tr = TimeRange(start=datetime(2000, 12, 31), end=datetime(2001, 1, 1))
    assert _months(tr) == [(2000, 12), (2001, 1)]


def test_native_gdex_typo_is_pinned():
    assert any("Radp" in mapping.source_name for mapping in _MAPPINGS)
