# SPDX-License-Identifier: MIT
"""Live integration tests for the auth-gated tiers (CDS + Earthdata).

Require credentials (~/.cdsapirc, ~/.netrc with urs.earthdata.nasa.gov) and the
'NASA GESDISC DATA ARCHIVE' app authorized under URS. Marked 'network'; CDS ones
are additionally slow (server-side queue).
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar

ALBERTA = BoundingBox(min_lon=-114.5, min_lat=50.7, max_lon=-114.0, max_lat=51.1)
COLORADO = BoundingBox(min_lon=-106.0, min_lat=39.8, max_lon=-105.5, max_lat=40.2)


def _sane_temp(ds):
    return 230.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 325.0


# ── Earthdata OPeNDAP tier ───────────────────────────────────────────

@pytest.mark.network
async def test_merra2_live():
    discover()
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 5))
    async with get_connector("merra2")() as c:
        ds, r = await c.fetch("merra2:single_levels", ALBERTA, tr,
                              variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX])
        ds = ds.load()
    assert _sane_temp(ds) and r.n_times > 0 and not r.warnings


@pytest.mark.network
async def test_nldas_live():
    discover()
    bbox = BoundingBox(min_lon=-100.0, min_lat=40.0, max_lon=-99.5, max_lat=40.4)
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 2))
    async with get_connector("nldas")() as c:
        ds, r = await c.fetch("nldas:fora0125_h", bbox, tr,
                              variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX])
        ds = ds.load()
    assert _sane_temp(ds) and r.n_times == 3


@pytest.mark.network
async def test_gpm_live():
    discover()
    tr = TimeRange(start=datetime(2015, 6, 1), end=datetime(2015, 6, 3))
    async with get_connector("gpm")() as c:
        ds, r = await c.fetch("gpm:imerg_daily", ALBERTA, tr)
        ds = ds.load()
    p = ds[CanonicalVar.PRECIPITATION_FLUX]
    assert r.n_times == 3 and float(p.min()) >= 0.0 and float(p.max()) < 0.05


@pytest.mark.network
async def test_daymet_live():
    discover()
    tr = TimeRange(start=datetime(2015, 6, 1), end=datetime(2015, 6, 3))
    async with get_connector("daymet")() as c:
        ds, r = await c.fetch("daymet:daily_v4", COLORADO, tr)
        ds = ds.load()
    assert _sane_temp(ds) and ds["latitude"].ndim == 2
    # Dewpoint must not exceed air temperature.
    assert float(ds[CanonicalVar.DEWPOINT_TEMPERATURE].max()) <= float(ds[CanonicalVar.AIR_TEMPERATURE].max()) + 1.0
    assert not r.warnings


# ── CDS tier (slow: server-side queue) ───────────────────────────────

@pytest.mark.network
async def test_era5_land_live():
    discover()
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6))
    async with get_connector("era5_land")() as c:
        ds, r = await c.fetch("era5_land:hourly", ALBERTA, tr,
                              variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                                         CanonicalVar.SHORTWAVE_RADIATION_DOWN])
        ds = ds.load()
    assert _sane_temp(ds) and r.n_times > 0 and not r.warnings


@pytest.mark.network
async def test_carra_live():
    discover()
    bbox = BoundingBox(min_lon=-52.0, min_lat=64.0, max_lon=-50.0, max_lat=65.0)  # Greenland (west_domain)
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6))
    async with get_connector("carra")() as c:
        ds, r = await c.fetch("carra:single_levels", bbox, tr,
                              variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                                         CanonicalVar.SPECIFIC_HUMIDITY])
        ds = ds.load()
    assert _sane_temp(ds) and r.n_times > 0 and not r.warnings


@pytest.mark.network
async def test_cerra_live():
    discover()
    bbox = BoundingBox(min_lon=6.0, min_lat=46.0, max_lon=8.0, max_lat=47.0)  # Alps
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6))
    async with get_connector("cerra")() as c:
        ds, r = await c.fetch("cerra:single_levels", bbox, tr,
                              variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.WIND_SPEED,
                                         CanonicalVar.SPECIFIC_HUMIDITY])
        ds = ds.load()
    assert _sane_temp(ds) and r.n_times > 0 and not r.warnings
