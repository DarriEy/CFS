# SPDX-License-Identifier: MIT
"""GPM IMERG offline tests: URL, precip-var detection, conversion, orientation."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.gpm import _MAPPINGS, _detect_precip, _opendap_url
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "gpm" in list_providers()


def test_url_structure_uses_year_month():
    # IMERG daily is laid out by /{year}/{month}/ (June -> 06).
    url = _opendap_url(2015, 6, "20150601")
    assert url.endswith(
        "/GPM_3IMERGDF.07/2015/06/3B-DAY.MS.MRG.3IMERG.20150601-S000000-E235959.V07B.nc4"
    )


# ── Precip variable detection (V07 vs older runs vs fallback) ─────────

def _ds_with(varname):
    return xr.Dataset(
        {varname: (("time", "lat", "lon"), np.ones((1, 2, 2)))},
        coords={"time": [0], "lat": [0.0, 0.1], "lon": [30.0, 30.1]},
    )


def test_detect_prefers_precipitation():
    ds = _ds_with("precipitation")
    assert _detect_precip(ds) == "precipitation"


def test_detect_falls_back_to_cal():
    ds = _ds_with("precipitationCal")
    assert _detect_precip(ds) == "precipitationCal"


def test_detect_fuzzy_match():
    ds = _ds_with("HQprecipitation")
    assert _detect_precip(ds) == "HQprecipitation"


def test_detect_none_when_absent():
    ds = xr.Dataset({"randomField": (("lat", "lon"), np.ones((2, 2)))},
                    coords={"lat": [0.0, 0.1], "lon": [30.0, 30.1]})
    assert _detect_precip(ds) is None


# ── Conversion + orientation ─────────────────────────────────────────

def test_daily_mm_to_flux():
    ds = xr.Dataset(
        {"precipitation": (("time", "lat", "lon"), np.full((1, 2, 2), 8.64))},  # mm/day
        coords={"time": [0], "lat": [0.0, 0.1], "lon": [30.0, 30.1]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(1e-4, rel=1e-6)  # 8.64 / 86400


def test_orientation_normalized_to_time_lat_lon():
    # IMERG can be dimensioned (time, lon, lat); after harmonize + transpose the
    # canonical cube must be (time, latitude, longitude).
    ds = xr.Dataset(
        {"precipitation": (("time", "lon", "lat"), np.full((1, 3, 2), 8.64))},
        coords={"time": [0], "lon": [30.0, 30.1, 30.2], "lat": [0.0, 0.1]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    out = out.transpose("time", "latitude", "longitude", ...)
    assert out[CanonicalVar.PRECIPITATION_FLUX].dims == ("time", "latitude", "longitude")
    assert out.sizes["latitude"] == 2 and out.sizes["longitude"] == 3
