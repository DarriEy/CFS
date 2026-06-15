# SPDX-License-Identifier: MIT
"""HRRR offline tests: registration, identity mappings, precip rejection, assembly."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.hrrr import (
    _FCST_FIELDS,
    _FCST_MAPPINGS,
    _FIELDS,
    _MAPPINGS,
    HRRRConnector,
    _fcst_max_lead,
    _fcst_url,
)
from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "hrrr" in list_providers()


def test_all_identity_mappings():
    # Every HRRR field (analysis and forecast) is already canonical SI → identity.
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _FCST_MAPPINGS)
    # Precipitation (PRATE) is forecast-only; the analysis stream has none.
    assert CanonicalVar.PRECIPITATION_FLUX not in {f[2] for f in _FIELDS}
    assert CanonicalVar.PRECIPITATION_FLUX in {f[2] for f in _FCST_FIELDS}


def test_forecast_products_listed():
    conn = HRRRConnector()
    import asyncio
    ids = {p.id for p in asyncio.run(conn.list_products())}
    assert ids == {"hrrr:sfc_anl", "hrrr:sfc_fcst"}


def test_fcst_url_and_lead_structure():
    cyc = datetime(2026, 6, 15, 11)   # an off-cycle hour → short horizon
    assert _fcst_url(cyc, 6).endswith("hrrr.20260615/conus/hrrr.t11z.wrfsfcf06.grib2")
    assert _fcst_max_lead(11) == 18           # non-extended cycle: f00–f18
    assert _fcst_max_lead(12) == 48           # 00/06/12/18Z: f00–f48
    assert _fcst_max_lead(0) == 48


async def test_precip_request_rejected():
    conn = HRRRConnector()
    bbox = BoundingBox(min_lon=-105.0, min_lat=40.0, max_lon=-104.5, max_lat=40.4)
    tr = TimeRange(start=datetime(2022, 1, 1, 0), end=datetime(2022, 1, 1, 1))
    with pytest.raises(SubsetError):
        await conn.fetch("hrrr:sfc_anl", bbox, tr, variables=[CanonicalVar.PRECIPITATION_FLUX])


def test_harmonize_assembled_cube_identity():
    # Mimic the per-hour assembly: data vars over (time, y, x) with 2-D lat/lon.
    ny, nx = 2, 3
    lat = np.linspace(40, 41, ny * nx).reshape(ny, nx)
    lon = np.linspace(-105, -104, ny * nx).reshape(ny, nx)
    ds = xr.Dataset(
        {
            "TMP": (("time", "y", "x"), np.full((1, ny, nx), 270.0)),
            "UGRD": (("time", "y", "x"), np.full((1, ny, nx), 5.0)),
            "DSWRF": (("time", "y", "x"), np.full((1, ny, nx), 200.0)),
        },
        coords={"latitude": (("y", "x"), lat), "longitude": (("y", "x"), lon), "time": [0]},
    )
    out = harmonize(ds, _MAPPINGS)  # lat/lon already canonical-named (2-D coords)
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(270.0)
    assert float(out[CanonicalVar.EASTWARD_WIND].values.flat[0]) == pytest.approx(5.0)
    assert out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].attrs["units"] == "W m-2"
    assert out["latitude"].dims == ("y", "x")  # 2-D coords preserved
