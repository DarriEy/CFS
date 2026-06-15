# SPDX-License-Identifier: MIT
"""Live HRRR integration test — hrrrzarr on AWS S3 (anonymous). Marked 'network'."""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("xarray")
pytest.importorskip("s3fs")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_hrrr_fetch_small_bbox():
    discover()
    conn_cls = get_connector("hrrr")
    bbox = BoundingBox(min_lon=-105.2, min_lat=39.9, max_lon=-104.7, max_lat=40.3)  # Colorado
    tr = TimeRange(start=datetime(2022, 1, 1, 0), end=datetime(2022, 1, 1, 2))

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "hrrr:sfc_anl", bbox, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SHORTWAVE_RADIATION_DOWN],
        )
        ds = ds.load()

    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SHORTWAVE_RADIATION_DOWN}
    assert result.n_times == 3  # hourly 00..02
    assert result.n_lat > 0 and result.n_lon > 0
    # lat/lon attached from the grid file, 2-D, and over the requested region.
    assert "latitude" in ds.coords and ds["latitude"].ndim == 2
    assert 39.0 < float(ds["latitude"].mean()) < 41.5
    assert -106.0 < float(ds["longitude"].mean()) < -104.0
    # Plausible January temperature; radiation a sane non-negative flux.
    assert 230.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 300.0
    assert float(ds[CanonicalVar.SHORTWAVE_RADIATION_DOWN].min()) >= 0.0
    assert float(ds[CanonicalVar.SHORTWAVE_RADIATION_DOWN].max()) < 1500.0


@pytest.mark.network
async def test_hrrr_forecast_grib_multilead():
    # GRIB2 forecast from noaa-hrrr-bdp-pds: one cycle, multiple leads (f00..f02).
    pytest.importorskip("cfgrib")
    discover()
    conn_cls = get_connector("hrrr")
    bbox = BoundingBox(min_lon=-105.2, min_lat=39.9, max_lon=-104.7, max_lat=40.3)  # Colorado
    tr = TimeRange(start=datetime(2022, 1, 1, 0), end=datetime(2022, 1, 1, 2))  # 00Z cycle, f00-f02

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "hrrr:sfc_fcst", bbox, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
        )
        ds = ds.load()

    # precipitation_flux is the forecast-only field; both come back from the GRIB path.
    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX}
    assert result.n_times == 3                          # f00, f01, f02 from the 00Z cycle
    assert "cycle 20220101 00z" in result.provenance
    assert ds["latitude"].ndim == 2                     # 2-D LCC grid
    assert 230.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 300.0
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0  # instantaneous PRATE flux
