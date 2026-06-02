# SPDX-License-Identifier: MIT
"""Offline checks for the WFDE5/gridMET/nClimGrid/CMORPH/NARR provider batch."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.cmorph import _MAPPINGS as CMORPH_MAPPINGS
from cfs.connectors.cmorph import _member_name, _parse_daily_tars
from cfs.connectors.gridmet import _MAPPINGS as GRIDMET_MAPPINGS
from cfs.connectors.gridmet import _TMEAN, _agg_url
from cfs.connectors.narr import _MAPPINGS as NARR_MAPPINGS
from cfs.connectors.narr import _yearly_url
from cfs.connectors.nclimgrid_daily import _MAPPINGS as NCLIM_MAPPINGS
from cfs.connectors.nclimgrid_daily import _monthly_url
from cfs.connectors.nclimgrid_daily import _months as nclim_months
from cfs.connectors.wfde5 import _MAPPINGS as WFDE5_MAPPINGS
from cfs.connectors.wfde5 import _PRECIP
from cfs.core.models import TimeRange
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_batch_providers_registered():
    discover()
    assert {"wfde5", "gridmet", "nclimgrid_daily", "cmorph", "narr"} <= set(list_providers())


@pytest.mark.parametrize(
    ("slug", "product_id"),
    [
        ("wfde5", "wfde5:hourly"),
        ("gridmet", "gridmet:daily"),
        ("nclimgrid_daily", "nclimgrid_daily:daily"),
        ("cmorph", "cmorph:daily"),
        ("narr", "narr:daily"),
    ],
)
async def test_batch_provider_catalogs(slug, product_id):
    conn_cls = get_connector(slug)
    async with conn_cls() as conn:
        products = await conn.list_products()
    assert [p.id for p in products] == [product_id]
    assert products[0].variables


def test_gridmet_urls_and_conversions():
    assert _agg_url("tmmx").endswith("/agg_met_tmmx_1979_CurrentYear_CONUS.nc")
    ds = xr.Dataset(
        {
            _TMEAN: (("day", "lat", "lon"), np.full((1, 2, 2), 290.0)),
            "precipitation_amount": (("day", "lat", "lon"), np.full((1, 2, 2), 8.64)),
        },
        coords={"day": [0], "lat": [40.0, 40.041], "lon": [-112.0, -111.959]},
    )
    out = harmonize(ds, GRIDMET_MAPPINGS, lat_name="lat", lon_name="lon", time_name="day")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(290.0)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4)
    assert "time" in out.coords


def test_nclimgrid_url_months_and_conversions():
    assert _monthly_url(2023, 1).endswith("/2023/ncdd-202301-grd-scaled.nc")
    tr = TimeRange(start=datetime(2022, 12, 31), end=datetime(2023, 2, 1))
    assert nclim_months(tr) == [(2022, 12), (2023, 1), (2023, 2)]
    ds = xr.Dataset(
        {
            "tavg": (("time", "lat", "lon"), np.full((1, 2, 2), 6.85)),
            "prcp": (("time", "lat", "lon"), np.full((1, 2, 2), 8.64)),
        },
        coords={"time": [0], "lat": [40.0, 40.041], "lon": [-112.0, -111.959]},
    )
    out = harmonize(ds, NCLIM_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(280.0)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4)


def test_cmorph_index_member_and_conversion():
    html = """
    <a href="cmorph_v1.0_0.25deg_daily_s20251201_e20251231_c20260505.tar">daily</a>
    <a href="cmorph_v1.0_0.25deg_hourly_s2025120100_e2025120123_c20260505.tar">hourly</a>
    """
    assert _parse_daily_tars(html) == {(2025, 12): "cmorph_v1.0_0.25deg_daily_s20251201_e20251231_c20260505.tar"}
    assert _member_name(2025, 12, 1) == "CMORPH_V1.0_ADJ_0.25deg-DLY_00Z_20251201.nc"
    ds = xr.Dataset(
        {"cmorph": (("time", "lat", "lon"), np.full((1, 2, 2), 8.64))},
        coords={"time": [0], "lat": [-10.0, -9.75], "lon": [100.0, 100.25]},
    )
    out = harmonize(ds, CMORPH_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4)


def test_wfde5_precip_combines_rain_and_snow():
    ds = xr.Dataset(
        {
            "Tair": (("time", "lat", "lon"), np.full((1, 2, 2), 280.0)),
            _PRECIP: (("time", "lat", "lon"), np.full((1, 2, 2), 3e-5)),
        },
        coords={"time": [0], "lat": [50.25, 50.75], "lon": [-114.75, -114.25]},
    )
    out = harmonize(ds, WFDE5_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(280.0)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(3e-5)


def test_narr_url_identity_and_2d_coords():
    assert _yearly_url("air.2m", 2021).endswith("/air.2m.2021.nc")
    lat2d = np.array([[45.0, 45.1], [45.2, 45.3]])
    lon2d = np.array([[-110.0, -109.9], [-109.8, -109.7]])
    ds = xr.Dataset(
        {
            "air": (("time", "y", "x"), np.full((1, 2, 2), 281.0)),
            "prate": (("time", "y", "x"), np.full((1, 2, 2), 5e-5)),
        },
        coords={"time": [0], "lat": (("y", "x"), lat2d), "lon": (("y", "x"), lon2d)},
    )
    out = harmonize(ds, NARR_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(281.0)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(5e-5)
    assert out["latitude"].dims == ("y", "x")
