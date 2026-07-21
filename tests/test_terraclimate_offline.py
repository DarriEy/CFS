# SPDX-License-Identifier: MIT
"""TerraClimate offline tests: products, mappings, URL form, monthly resolution."""

from __future__ import annotations

import asyncio
from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.terraclimate import (
    _MAPPINGS,
    _SOURCE_VARS,
    TerraClimateConnector,
    _agg_url,
)
from cfs.core.models import BoundingBox, TemporalResolution, TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "terraclimate" in list_providers()


def test_one_monthly_product():
    products = asyncio.run(TerraClimateConnector().list_products())
    assert [p.id for p in products] == ["terraclimate:monthly"]
    assert products[0].temporal.resolution == TemporalResolution.MONTHLY


def test_source_var_dependencies():
    # air_temperature derives from tmax+tmin; precip from ppt; others identity.
    assert _SOURCE_VARS[CanonicalVar.AIR_TEMPERATURE] == ("tmax", "tmin")
    assert _SOURCE_VARS[CanonicalVar.PRECIPITATION_FLUX] == ("ppt",)
    assert _SOURCE_VARS[CanonicalVar.SHORTWAVE_RADIATION_DOWN] == ("srad",)
    assert _SOURCE_VARS[CanonicalVar.WIND_SPEED] == ("ws",)


def test_mappings_units():
    by_canon = {m.canonical: m for m in _MAPPINGS}
    # Mean temperature derived field → degC to K offset, no scale.
    assert by_canon[CanonicalVar.AIR_TEMPERATURE].source_name == "tair"
    assert by_canon[CanonicalVar.AIR_TEMPERATURE].offset == 273.15
    # Precip flux is pre-divided in-connector → identity mapping of ppt_flux.
    assert by_canon[CanonicalVar.PRECIPITATION_FLUX].source_name == "ppt_flux"
    assert by_canon[CanonicalVar.PRECIPITATION_FLUX].scale == 1.0
    # srad / ws are identity SI.
    assert by_canon[CanonicalVar.SHORTWAVE_RADIATION_DOWN].scale == 1.0
    assert by_canon[CanonicalVar.WIND_SPEED].source_name == "ws"


def test_agg_url_uses_underscore_form():
    # All dodsC aggregations use the underscore `_1950_CurrentYear` form, even
    # srad/swe (whose HTML-listing names use a hyphen).
    assert _agg_url("tmax").endswith(
        "/agg_terraclimate_tmax_1950_CurrentYear_GLOBE.nc"
    )
    assert _agg_url("srad").endswith(
        "/agg_terraclimate_srad_1950_CurrentYear_GLOBE.nc"
    )
    assert _agg_url("ppt").startswith("http://thredds.northwestknowledge.net:8080/")


def _source_cube(var: str, values: list[float]):
    times = np.array(["2020-02-01", "2020-03-01"], dtype="datetime64[ns]")
    data = np.asarray(values, dtype="float64")[:, None, None]
    return xr.Dataset(
        {var: (("time", "lat", "lon"), data)},
        coords={"time": times, "lat": [50.0], "lon": [-110.0], "crs": 0},
    )


def test_fetch_calendar_derivations_and_recovery_after_partial_failure(monkeypatch):
    """A failed source open must not poison a complete retry."""
    from cfs.connectors import terraclimate

    cubes = {
        "tmax": _source_cube("tmax", [10.0, 14.0]),
        "tmin": _source_cube("tmin", [0.0, 2.0]),
        "ppt": _source_cube("ppt", [290.0, 310.0]),
    }
    calls: list[str] = []
    fail_once = {"ppt": True}

    def open_source(url: str):
        var = next(name for name in cubes if f"_{name}_" in url)
        calls.append(var)
        if var == "ppt" and fail_once.pop("ppt", False):
            raise OSError("transient source failure")
        return cubes[var].copy(deep=True)

    monkeypatch.setattr(terraclimate, "_open_anonymous_opendap", open_source)
    connector = TerraClimateConnector()
    bbox = BoundingBox(min_lon=-111, min_lat=49, max_lon=-109, max_lat=51)
    period = TimeRange(start=datetime(2020, 2, 1), end=datetime(2020, 3, 1))
    wanted = [CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX]

    with pytest.raises(OSError, match="transient source failure"):
        asyncio.run(connector.fetch("terraclimate:monthly", bbox, period, wanted))
    dataset, result = asyncio.run(
        connector.fetch("terraclimate:monthly", bbox, period, wanted)
    )

    np.testing.assert_allclose(
        dataset[CanonicalVar.AIR_TEMPERATURE.value].values[:, 0, 0],
        [278.15, 281.15],
    )
    expected_flux = np.array([290.0 / (29 * 86400), 310.0 / (31 * 86400)])
    np.testing.assert_allclose(
        dataset[CanonicalVar.PRECIPITATION_FLUX.value].values[:, 0, 0],
        expected_flux,
    )
    assert calls == ["tmax", "tmin", "ppt", "tmax", "tmin", "ppt"]
    assert result.n_times == 2
    assert result.variables == wanted
