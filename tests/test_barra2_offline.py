# SPDX-License-Identifier: MIT
"""BARRA2 offline tests: registration, URL building, month span, mappings."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.barra2 import _MAPPINGS, _months, _ncss_url
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "barra2" in set(list_providers())


def test_barra2_ncss_url_structure():
    bbox = BoundingBox(min_lon=148.0, min_lat=-36.0, max_lon=149.5, max_lat=-35.0)
    url = _ncss_url("pr", 2020, 6, bbox, "2020-06-01T00:00:00Z", "2020-06-01T07:00:00Z")
    assert url.startswith("https://thredds.nci.org.au/thredds/ncss/grid/ob53/BARRA2/")
    assert "/1hr/pr/latest/pr_AUS-11_ERA5_historical_hres_BOM_BARRA-R2_v1_1hr_202006-202006.nc?" in url
    assert "var=pr" in url and "accept=netcdf4" in url
    assert "north=-35.0" in url and "south=-36.0" in url
    # time window is required (ncss returns only the last step otherwise).
    assert "time_start=2020-06-01T00:00:00Z" in url and "time_end=2020-06-01T07:00:00Z" in url


def test_barra2_ncss_longitude_normalized_to_0_360():
    # A box straddling 180° in -180/180 terms maps to ordered 0-360 west<east.
    bbox = BoundingBox(min_lon=175.0, min_lat=-40.0, max_lon=-175.0, max_lat=-30.0)
    url = _ncss_url("tas", 2020, 1, bbox, "2020-01-01T00:00:00Z", "2020-01-01T01:00:00Z")
    assert "west=175.0" in url and "east=185.0" in url


def test_barra2_month_span_inclusive():
    tr = TimeRange(start=datetime(2019, 11, 15), end=datetime(2020, 2, 3))
    assert _months(tr) == [(2019, 11), (2019, 12), (2020, 1), (2020, 2)]


def test_barra2_single_month():
    tr = TimeRange(start=datetime(2020, 6, 1), end=datetime(2020, 6, 30))
    assert _months(tr) == [(2020, 6)]


async def test_barra2_one_product():
    Conn = get_connector("barra2")
    async with Conn() as conn:
        prods = await conn.list_products()
    assert [p.id for p in prods] == ["barra2:barra_r2"]


def test_barra2_all_identity_mappings():
    # BARRA-R2 uses CORDEX/CMIP CF names already in SI → scale 1, offset 0.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in _MAPPINGS)
    canon = {m.canonical for m in _MAPPINGS}
    # vector winds (no scalar wind_speed), no dewpoint (tdps not published).
    assert CanonicalVar.EASTWARD_WIND in canon and CanonicalVar.NORTHWARD_WIND in canon
    assert CanonicalVar.DEWPOINT_TEMPERATURE not in canon
    assert {m.source_name for m in _MAPPINGS} == {
        "tas", "huss", "ps", "uas", "vas", "rsds", "rlds", "pr"
    }


def test_barra2_precip_is_identity_flux():
    # pr is already precipitation_flux (kg m-2 s-1): no scaling, no accumulation.
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {"pr": (("time", "lat", "lon"), np.full(shape, 3e-5))},
        coords={"time": [0], "lat": [-35.0, -34.89], "lon": [148.0, 148.11]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(3e-5)
    assert "latitude" in out.coords and "longitude" in out.coords
