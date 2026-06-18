# SPDX-License-Identifier: MIT
"""TerraClimate offline tests: products, mappings, URL form, monthly resolution."""

from __future__ import annotations

import asyncio

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.terraclimate import (
    _MAPPINGS,
    _SOURCE_VARS,
    TerraClimateConnector,
    _agg_url,
)
from cfs.core.models import TemporalResolution
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
