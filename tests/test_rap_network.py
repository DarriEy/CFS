# SPDX-License-Identifier: MIT
"""Live RAP integration test — noaa-rap-pds GRIB2 on AWS S3 (anonymous). Marked 'network'."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

pytest.importorskip("xarray")
pytest.importorskip("cfgrib")

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar


@pytest.mark.network
async def test_rap_forecast_multilead_with_radiation_and_winds():
    discover()
    conn_cls = get_connector("rap")
    bbox = BoundingBox(min_lon=-105.2, min_lat=39.9, max_lon=-104.7, max_lat=40.3)  # Colorado
    tr = TimeRange(start=datetime(2022, 1, 1, 0), end=datetime(2022, 1, 1, 2))  # 00Z cycle, f00-f02

    async with conn_cls() as conn:
        ds, result = await conn.fetch(
            "rap:awp130_fcst", bbox, tr,
            variables=[
                CanonicalVar.AIR_TEMPERATURE,
                CanonicalVar.PRECIPITATION_FLUX,
                CanonicalVar.SHORTWAVE_RADIATION_DOWN,  # from the bgrb file
                # Winds REQUESTED on purpose: RAP packs 10 m U+V in one GRIB message.
                CanonicalVar.EASTWARD_WIND,
                CanonicalVar.NORTHWARD_WIND,
            ],
        )
        ds = ds.load()

    # Radiation (bgrb) merges with T/precip/wind (pgrb) into one cube across leads.
    assert set(ds.data_vars) == {
        CanonicalVar.AIR_TEMPERATURE,
        CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND,
    }
    assert result.n_times == 3                          # f00, f01, f02 from the 00Z cycle
    assert "cycle 20220101 00z" in result.provenance
    assert ds["latitude"].ndim == 2                     # 2-D LCC grid
    assert 230.0 < float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) < 300.0
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].min()) >= 0.0
    assert float(ds[CanonicalVar.SHORTWAVE_RADIATION_DOWN].min()) >= 0.0
    # U and V from the combined message: distinct components, finite, plausible.
    u = ds[CanonicalVar.EASTWARD_WIND].values
    v = ds[CanonicalVar.NORTHWARD_WIND].values
    assert np.isfinite(u).any() and not np.allclose(u, v)
    assert float(np.nanmax(np.abs(u))) < 120.0 and float(np.nanmax(np.abs(v))) < 120.0
