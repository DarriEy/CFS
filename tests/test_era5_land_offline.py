# SPDX-License-Identifier: MIT
"""ERA5-Land + CDS-mixin offline tests (no credentials, no network)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.era5_land import _MAPPINGS, ERA5LandConnector
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize

# ── CDS request-helper logic ─────────────────────────────────────────

def test_cds_area_order_is_north_west_south_east():
    conn = ERA5LandConnector()
    bbox = BoundingBox(min_lon=-114.5, min_lat=50.5, max_lon=-114.0, max_lat=51.2)
    assert conn._cds_area(bbox) == [51.2, -114.5, 50.5, -114.0]


def test_month_chunks_span_inclusive():
    conn = ERA5LandConnector()
    tr = TimeRange(start=datetime(2019, 11, 15), end=datetime(2020, 2, 3))
    assert conn._month_chunks(tr) == [(2019, 11), (2019, 12), (2020, 1), (2020, 2)]


def test_cache_name_varies_with_bbox():
    conn = ERA5LandConnector()
    b1 = BoundingBox(min_lon=-114.5, min_lat=50.5, max_lon=-114.0, max_lat=51.2)
    b2 = BoundingBox(min_lon=-115.0, min_lat=50.5, max_lon=-114.0, max_lat=51.2)
    n1 = conn._cache_name(b1, ["2m_temperature"], 2020, 1)
    n2 = conn._cache_name(b2, ["2m_temperature"], 2020, 1)
    assert n1 != n2  # different bbox → different cache file (no stale reuse)


# ── End-to-end harmonization on a synthetic ERA5-Land-like cube ───────

def _era5_land_like():
    """Short names + daily-reset accumulation, like a CDS NetCDF (3 hours)."""
    t = np.arange(3)
    shape = (3, 1, 1)
    return xr.Dataset(
        {
            "t2m": (("time", "latitude", "longitude"), np.full(shape, 283.0)),
            # tp accumulates 0.001, 0.002, 0.003 m over the day (no reset here).
            "tp": (("time", "latitude", "longitude"),
                   np.array([0.001, 0.002, 0.003]).reshape(shape)),
            # ssrd accumulates 0, 3600, 7200 J/m2.
            "ssrd": (("time", "latitude", "longitude"),
                     np.array([0.0, 3600.0, 7200.0]).reshape(shape)),
        },
        coords={"time": t, "latitude": [50.5], "longitude": [-114.0]},
    )


def test_era5_land_deaccumulation_and_units():
    out = harmonize(_era5_land_like(), _MAPPINGS)
    # tp increments: 0.001, 0.001, 0.001 m/hr -> *1000/3600 = 2.778e-4 kg m-2 s-1
    tp = out[CanonicalVar.PRECIPITATION_FLUX].values.ravel()
    assert tp[1] == pytest.approx(0.001 * 1000.0 / 3600.0, rel=1e-6)
    assert tp[2] == pytest.approx(0.001 * 1000.0 / 3600.0, rel=1e-6)
    # ssrd increments: 0, 3600, 3600 J/m2/hr -> /3600 = 0, 1, 1 W m-2
    sw = out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].values.ravel()
    assert sw[1] == pytest.approx(1.0, rel=1e-6)
    assert sw[2] == pytest.approx(1.0, rel=1e-6)
    # temperature is identity.
    assert out[CanonicalVar.AIR_TEMPERATURE].attrs["units"] == "K"
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(283.0)


def test_era5_land_missing_credentials_raises_clear_error(monkeypatch):
    from cfs.core.exceptions import RegistrationRequiredError

    pytest.importorskip("cdsapi")
    conn = ERA5LandConnector()
    monkeypatch.setattr(conn, "_cds_credentials_present", lambda: False)
    with pytest.raises(RegistrationRequiredError):
        conn._cds_client()
