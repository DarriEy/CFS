# SPDX-License-Identifier: MIT
"""Live NAM integration test — noaa-nam-pds GRIB2 on AWS S3 (anonymous). Marked 'network'.

Includes a closed-loop precipitation check: the sum of the connector's
de-accumulated hourly increments over [3, 6] must equal NAM's own independent
"3-6 hour acc" APCP bucket, validating the de-accumulation end-to-end.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("xarray")
pytest.importorskip("cfgrib")

from cfs.connectors.protocols.grib_idx import http_range, parse_idx_records, read_message_2d
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar

_CYCLE = datetime(2026, 6, 13, 12)            # a recent 12Z cycle in the rolling archive
_BBOX = BoundingBox(min_lon=-82.0, min_lat=25.0, max_lon=-80.0, max_lat=27.0)  # Florida


@pytest.mark.network
async def test_nam_forecast_fields_and_grid():
    discover()
    conn_cls = get_connector("nam")
    tr = TimeRange(start=_CYCLE + timedelta(hours=1), end=_CYCLE + timedelta(hours=3))
    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "nam:awphys_fcst", _BBOX, tr,
            # Winds are REQUESTED here on purpose: NAM packs 10 m U+V in one GRIB
            # message, a case that the byte-range-per-field model used to mishandle.
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SHORTWAVE_RADIATION_DOWN,
                       CanonicalVar.PRECIPITATION_FLUX,
                       CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND],
        )
        ds = ds.load()
    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE,
                                 CanonicalVar.SHORTWAVE_RADIATION_DOWN,
                                 CanonicalVar.PRECIPITATION_FLUX,
                                 CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND}
    assert result.n_times == 3
    assert "cycle 20260613 12z" in result.provenance
    assert ds["latitude"].ndim == 2
    assert 270.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 320.0
    # U and V come from the same combined message; they must be the distinct
    # components (not the same one twice), and finite.
    u = ds[CanonicalVar.EASTWARD_WIND].values
    v = ds[CanonicalVar.NORTHWARD_WIND].values
    assert np.isfinite(u).any() and not np.allclose(u, v)
    assert float(np.nanmax(np.abs(u))) < 120.0 and float(np.nanmax(np.abs(v))) < 120.0


@pytest.mark.network
async def test_nam_conusnest_instantaneous_fields():
    # 3 km CONUS nest: all-instantaneous identity SI (PRATE flux, no APCP de-accum),
    # incl. the combined U/V winds.
    discover()
    conn_cls = get_connector("nam")
    tr = TimeRange(start=_CYCLE + timedelta(hours=4), end=_CYCLE + timedelta(hours=6))
    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "nam:conusnest_fcst", _BBOX, tr,
            variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                       CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND],
        )
        ds = ds.load()
    assert set(ds.data_vars) == {CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX,
                                 CanonicalVar.EASTWARD_WIND, CanonicalVar.NORTHWARD_WIND}
    assert result.n_times == 3
    assert "conusnest" in result.provenance
    assert ds["latitude"].ndim == 2
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0
    assert not np.allclose(ds[CanonicalVar.EASTWARD_WIND].values,
                           ds[CanonicalVar.NORTHWARD_WIND].values)
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0


@pytest.mark.network
async def test_nam_precip_deaccumulation_closed_loop():
    # Sum of connector hourly increments over [3,6] must equal NAM's own 3-6h bucket.
    discover()
    conn_cls = get_connector("nam")
    tr = TimeRange(start=_CYCLE + timedelta(hours=4), end=_CYCLE + timedelta(hours=6))
    async with conn_cls() as conn:
        ds, _ = await conn.fetch("nam:awphys_fcst", _BBOX, tr,
                                 variables=[CanonicalVar.PRECIPITATION_FLUX])
        ds = ds.load()
    conn_total_mm = float(np.nansum(ds[CanonicalVar.PRECIPITATION_FLUX].values, axis=0).mean() * 3600.0)

    url = (f"https://noaa-nam-pds.s3.amazonaws.com/nam.{_CYCLE:%Y%m%d}/"
           f"nam.t{_CYCLE:%H}z.awphys06.tm00.grib2")
    recs = parse_idx_records(http_range(url + ".idx", 0, "").decode())
    rng = next(((sb, e) for v, lv, sb, e, fc in recs
                if v == "APCP" and lv == "surface" and fc == "3-6 hour acc fcst"), None)
    assert rng is not None
    bucket = read_message_2d(url, rng[0], rng[1], "APCP", _BBOX, label="NAM").load()
    ref_mm = float(bucket["APCP"].mean())

    assert conn_total_mm == pytest.approx(ref_mm, abs=1e-3)
