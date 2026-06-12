# SPDX-License-Identifier: MIT
"""Daymet offline tests: dewpoint inversion, derived fields, LCC projection."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.daymet import _MAPPINGS, _PR, _SW, _T, _TD, DaymetConnector
from cfs.core.models import BoundingBox
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.derive.humidity import (
    dewpoint_from_vapor_pressure,
    saturation_vapor_pressure,
)
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "daymet" in list_providers()


def test_dewpoint_inverts_saturation():
    # Round-trip: e_s(Td) -> Td must recover the original dewpoint.
    for td_k in (273.15, 283.15, 293.15):
        vp = saturation_vapor_pressure(td_k)
        assert dewpoint_from_vapor_pressure(vp) == pytest.approx(td_k, abs=1e-6)


def test_bbox_to_lcc_ordering():
    conn = DaymetConnector()
    bbox = BoundingBox(min_lon=-106.0, min_lat=39.8, max_lon=-105.5, max_lat=40.2)
    x_min, x_max, y_min, y_max = conn._bbox_to_lcc(bbox)
    assert x_min < x_max and y_min < y_max


def test_daymet_derived_fields():
    nt, ny, nx = 2, 2, 3
    def f(v):
        return (("time", "y", "x"), np.full((nt, ny, nx), v))
    ds = xr.Dataset(
        {
            "tmax": f(20.0), "tmin": f(10.0),  # °C → mean 15 °C = 288.15 K
            "prcp": f(8.64),                    # mm/day → 1e-4 kg m-2 s-1
            "srad": f(400.0), "dayl": f(43200.0),  # 400 W/m² over 12 h → 200 W/m²
            "vp": f(1227.0),                    # ≈ dewpoint 10 °C = 283.15 K
        },
        coords={
            "lat": (("y", "x"), np.full((ny, nx), 40.0)),
            "lon": (("y", "x"), np.full((ny, nx), -105.0)),
            "time": np.arange(nt),
        },
    )
    ds = ds.assign({
        _T: (ds.tmax + ds.tmin) / 2.0 + 273.15,
        _TD: dewpoint_from_vapor_pressure(ds.vp),
        _PR: ds.prcp / 86400.0,
        _SW: ds.srad * ds.dayl / 86400.0,
    })
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")

    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(288.15)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4, rel=1e-6)
    assert float(out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].values.flat[0]) == pytest.approx(200.0)
    assert float(out[CanonicalVar.DEWPOINT_TEMPERATURE].values.flat[0]) == pytest.approx(283.15, abs=0.1)
    # Derivation inputs must not leak into the canonical output.
    assert "tmax" not in out.data_vars and "vp" not in out.data_vars


def test_daymet_offers_no_wind_or_pressure():
    canon = {m.canonical for m in _MAPPINGS}
    assert CanonicalVar.EASTWARD_WIND not in canon
    assert CanonicalVar.SURFACE_AIR_PRESSURE not in canon
    assert CanonicalVar.SPECIFIC_HUMIDITY not in canon  # would need pressure→elevation


def test_sanitize_grid_mapping_attrs_keeps_netcdf_serializable():
    """pydap leaks the grid-mapping container as a dict attr; it must go."""
    from cfs.connectors.daymet import DAYMET_PROJ4, _sanitize_grid_mapping_attrs

    ds = xr.Dataset(
        {"air_temperature": (("time",), np.array([280.0], dtype="float32"))},
        coords={"time": [0]},
        attrs={
            "lambert_conformal_conic": {"grid_mapping_name": "lambert_conformal_conic"},
            "source": "Daymet V4R1",
        },
    )
    out = _sanitize_grid_mapping_attrs(ds)
    assert "lambert_conformal_conic" not in out.attrs
    assert out.attrs["source"] == "Daymet V4R1"  # plain attrs survive
    assert out.attrs["daymet_lcc_proj4"] == DAYMET_PROJ4
    assert not any(isinstance(v, dict) for v in out.attrs.values())
