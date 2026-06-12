# SPDX-License-Identifier: MIT
"""CARRA/CERRA offline tests: request shape, stream merge, harmonization."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.carra import CARRAConnector
from cfs.connectors.cerra import CERRAConnector
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.derive.humidity import specific_humidity_from_rh
from cfs.subset.canonical import VariableMapping, harmonize

BBOX = BoundingBox(min_lon=-60.0, min_lat=60.0, max_lon=-40.0, max_lat=70.0)


def test_registered():
    discover()
    assert {"carra", "cerra"} <= set(list_providers())


# ── Request building ─────────────────────────────────────────────────

def test_carra_request_has_domain_and_grid_no_leadtime_for_analysis():
    req = CARRAConnector()._build_request("analysis", BBOX, 2020, 1, ["2m_temperature"], ["01"])
    assert req["domain"] == "west_domain"
    assert req["grid"] == [0.025, 0.025]
    assert req["area"] == [70.0, -60.0, 60.0, -40.0]  # [N, W, S, E]
    assert req["level_type"] == "surface_or_atmosphere"
    assert req["day"] == ["01"]
    assert "leadtime_hour" not in req


def test_carra_forecast_request_has_leadtime():
    req = CARRAConnector()._build_request("forecast", BBOX, 2020, 1, ["total_precipitation"], ["01"])
    assert req["leadtime_hour"] == "1"
    assert req["product_type"] == "forecast"


def test_carra_domain_override():
    conn = CARRAConnector(config={"domain": "east_domain"})
    req = conn._build_request("analysis", BBOX, 2020, 1, ["2m_temperature"], ["01"])
    assert req["domain"] == "east_domain"


def test_longwave_cds_names_differ_between_carra_and_cerra():
    """Live-confirmed (2026-06-12 parity campaign): CARRA's CDS form names
    downwelling longwave `thermal_surface_radiation_downwards`, CERRA's names
    it `surface_thermal_radiation_downwards` — and CDS SILENTLY DROPS unknown
    variable names instead of rejecting the request, so the wrong name yields
    files with no longwave. Pin both."""
    carra_lw = [v.request_name for v in CARRAConnector.forecast_vars if v.nc_name == "strd"]
    cerra_lw = [v.request_name for v in CERRAConnector.forecast_vars if v.nc_name == "strd"]
    assert carra_lw == ["thermal_surface_radiation_downwards"]
    assert cerra_lw == ["surface_thermal_radiation_downwards"]


def test_cerra_request_has_data_type_no_domain():
    req = CERRAConnector()._build_request("analysis", BBOX, 2020, 1, ["2m_temperature"], ["01"])
    assert req["data_type"] == "reanalysis"
    assert req["grid"] == [0.05, 0.05]
    assert "domain" not in req


def test_chunk_days_limits_to_window():
    from datetime import datetime
    # A 1-day request must ask CDS for only that day, not the whole month.
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6))
    assert CARRAConnector._chunk_days(2015, 6, tr) == ["01"]
    # A cross-month range yields each month's in-window days.
    tr2 = TimeRange(start=datetime(2015, 5, 30, 0), end=datetime(2015, 6, 2, 0))
    assert CARRAConnector._chunk_days(2015, 5, tr2) == ["30", "31"]
    assert CARRAConnector._chunk_days(2015, 6, tr2) == ["01", "02"]


# ── Forecast alignment + stream merge ────────────────────────────────

def _cube(varnames, times):
    t = np.array(times, dtype="datetime64[ns]")
    data = {v: (("time", "latitude", "longitude"), np.ones((len(t), 1, 1))) for v in varnames}
    return xr.Dataset(data, coords={"time": t, "latitude": [65.0], "longitude": [-50.0]})


def test_align_forecast_shifts_back_one_hour():
    fc = _cube(["tp"], ["2020-01-01T01:00", "2020-01-01T04:00"])
    aligned = CARRAConnector._align_forecast(fc, 1)
    assert aligned["time"].values[0] == np.datetime64("2020-01-01T00:00")
    assert aligned["time"].values[1] == np.datetime64("2020-01-01T03:00")


def test_merge_streams_inner_join_on_aligned_times():
    an = _cube(["t2m"], ["2020-01-01T00:00", "2020-01-01T03:00"])
    fc = _cube(["tp"], ["2020-01-01T01:00", "2020-01-01T04:00"])
    fc = CARRAConnector._align_forecast(fc, 1)
    merged = CARRAConnector._merge_streams(an, fc)
    assert "t2m" in merged.data_vars and "tp" in merged.data_vars
    assert merged.sizes["time"] == 2  # 00 and 03 are shared after alignment


# ── End-to-end harmonization with derived specific humidity ──────────

def _carra_like():
    t = np.array(["2020-06-01T00:00", "2020-06-01T03:00"], dtype="datetime64[ns]")
    shape = (2, 1, 1)
    def f(v):
        return (("time", "latitude", "longitude"), np.full(shape, v))
    return xr.Dataset(
        {
            "t2m": f(283.0), "u10": f(3.0), "v10": f(-4.0), "sp": f(98000.0),
            "r2": f(80.0),  # 80% RH
            "tp": f(3.6),     # kg/m2 over 1 h  → /3600 = 0.001
            "ssrd": f(1800000.0),  # J/m2 over 1 h → /3600 = 500 W/m2
            "strd": f(1080000.0),  # → 300 W/m2
        },
        coords={"time": t, "latitude": [65.0], "longitude": [-50.0]},
    )


def test_carra_full_harmonization_with_derived_q():
    conn = CARRAConnector()
    ds = _carra_like()
    q = specific_humidity_from_rh(ds["r2"], ds["t2m"], ds["sp"])
    ds = ds.assign(specific_humidity_derived=q)
    mappings = [
        VariableMapping(v.nc_name, v.canonical, scale=v.scale)
        for v in (*conn.analysis_vars, *conn.forecast_vars)
    ] + [VariableMapping("specific_humidity_derived", CanonicalVar.SPECIFIC_HUMIDITY)]

    out = harmonize(ds, mappings)

    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(0.001, rel=1e-6)
    assert float(out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].values.flat[0]) == pytest.approx(500.0, rel=1e-6)
    assert float(out[CanonicalVar.LONGWAVE_RADIATION_DOWN].values.flat[0]) == pytest.approx(300.0, rel=1e-6)
    assert float(out[CanonicalVar.EASTWARD_WIND].values.flat[0]) == pytest.approx(3.0)
    # Derived specific humidity present, sane, and labelled in canonical units.
    qv = float(out[CanonicalVar.SPECIFIC_HUMIDITY].values.flat[0])
    assert 0.0 < qv < 0.02
    assert out[CanonicalVar.SPECIFIC_HUMIDITY].attrs["units"] == "kg kg-1"
    # RH was an input only — it must not leak into the canonical output.
    assert "r2" not in out.data_vars


def test_cerra_offers_wind_speed_not_components():
    conn = CERRAConnector()
    canon = {v.canonical for v in conn.analysis_vars}
    assert CanonicalVar.WIND_SPEED in canon
    assert CanonicalVar.EASTWARD_WIND not in canon
