# SPDX-License-Identifier: MIT
from datetime import datetime

import pytest

pytest.importorskip("xarray")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_cfsr_historical_companion_live():
    discover()
    connector = get_connector("cfsr")()
    bbox = BoundingBox(min_lon=-105.4, min_lat=39.7, max_lon=-105.0, max_lat=40.1)
    time_range = TimeRange(start=datetime(2000, 1, 1), end=datetime(2000, 1, 1, 1))
    async with connector:
        ds, result = await connector.fetch(
            "cfsr:hourly_timeseries",
            bbox,
            time_range,
            variables=[CanonicalVar.AIR_TEMPERATURE],
        )
    assert ds.sizes["time"] >= 1
    assert ds.sizes["latitude"] > 0 and ds.sizes["longitude"] > 0
    assert float(ds.air_temperature.min()) > 180.0
    assert result.provider == "cfsr"
