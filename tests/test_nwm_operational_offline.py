# SPDX-License-Identifier: MIT
"""NWM Operational offline tests: products, mappings, harmonization."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.nwm_operational import (
    _MAPPINGS,
    NWMOperationalConnector,
    _analysis_path,
    _floor_cycle,
    _forecast_path,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "nwm_operational" in list_providers()


async def test_products():
    conn = NWMOperationalConnector()
    products = await conn.list_products()
    ids = {p.id for p in products}
    # analysis_assim (tm00 valid-time forcing) plus the two deterministic forecast
    # configs resolved via a cycle×lead resolver.
    assert ids == {
        "nwm_operational:analysis_assim",
        "nwm_operational:short_range",
        "nwm_operational:medium_range",
    }


def test_floor_cycle_cadence():
    # short_range cycles hourly; medium_range every 6 h.
    assert _floor_cycle(datetime(2026, 6, 15, 13, 42), 1) == datetime(2026, 6, 15, 13)
    assert _floor_cycle(datetime(2026, 6, 15, 13, 42), 6) == datetime(2026, 6, 15, 12)
    assert _floor_cycle(datetime(2026, 6, 15, 5, 59), 6) == datetime(2026, 6, 15, 0)


def test_path_patterns_match_live_layout():
    ts = datetime(2026, 6, 15, 0)
    assert _analysis_path(ts).endswith(
        "nwm.20260615/forcing_analysis_assim/nwm.t00z.analysis_assim.forcing.tm00.conus.nc"
    )
    assert _forecast_path("short_range", ts, 6).endswith(
        "nwm.20260615/forcing_short_range/nwm.t00z.short_range.forcing.f006.conus.nc"
    )
    assert _forecast_path("medium_range", datetime(2026, 6, 15, 12), 240).endswith(
        "nwm.20260615/forcing_medium_range/nwm.t12z.medium_range.forcing.f240.conus.nc"
    )


def test_mappings_identity():
    # NWM forcing is native SI.
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    canon = {m.canonical for m in _MAPPINGS}
    assert {CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.AIR_TEMPERATURE,
            CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.SURFACE_AIR_PRESSURE} <= canon


def test_harmonization_2d_lcc():
    # LDASIN files have 2-D lat/lon and y/x dims.
    shape = (1, 2, 2)
    lat2d = np.array([[40.0, 40.01], [40.02, 40.03]])
    lon2d = np.array([[-105.0, -104.99], [-104.98, -104.97]])
    ds = xr.Dataset(
        {
            "T2D": (("time", "y", "x"), np.full(shape, 290.0)),
            "RAINRATE": (("time", "y", "x"), np.full(shape, 1e-5)),
        },
        coords={
            "time": [0],
            "lat": (("y", "x"), lat2d),
            "lon": (("y", "x"), lon2d),
        },
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-5)
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(290.0)
    assert out["latitude"].dims == ("y", "x")
