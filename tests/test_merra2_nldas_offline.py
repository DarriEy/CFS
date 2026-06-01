# SPDX-License-Identifier: MIT
"""MERRA-2 / NLDAS offline tests: URL building, stream map, mappings."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.merra2 import _MAPPINGS as MERRA2_MAPPINGS
from cfs.connectors.merra2 import _opendap_url as merra2_url
from cfs.connectors.merra2 import _stream
from cfs.connectors.nldas import _MAPPINGS as NLDAS_MAPPINGS
from cfs.connectors.nldas import _opendap_url as nldas_url
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert {"merra2", "nldas"} <= set(list_providers())


# ── MERRA-2 URL / stream logic ───────────────────────────────────────

@pytest.mark.parametrize("year,stream", [(1985, 100), (1995, 200), (2005, 300), (2020, 400)])
def test_merra2_stream_map(year, stream):
    assert _stream(year) == stream


def test_merra2_url_structure():
    url = merra2_url("M2T1NXSLV.5.12.4", "tavg1_2d_slv_Nx", 2015, 6, 1)
    assert url.endswith("/M2T1NXSLV.5.12.4/2015/06/MERRA2_400.tavg1_2d_slv_Nx.20150601.nc4")


def test_merra2_all_identity_mappings():
    # Every MERRA-2 field is already canonical SI → scale 1, offset 0.
    assert all(m.scale == 1.0 and m.offset == 0.0 for m in MERRA2_MAPPINGS)


def test_merra2_harmonization_identity():
    shape = (2, 2, 2)
    ds = xr.Dataset(
        {
            "T2M": (("time", "lat", "lon"), np.full(shape, 288.0)),
            "PRECTOTCORR": (("time", "lat", "lon"), np.full(shape, 1e-5)),
            "U10M": (("time", "lat", "lon"), np.full(shape, 4.0)),
        },
        coords={"time": [0, 1], "lat": [40.0, 40.5], "lon": [-100.0, -99.5]},
    )
    out = harmonize(ds, MERRA2_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(288.0)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-5)
    # lat/lon coords standardized to canonical names by harmonize.
    assert "latitude" in out.coords and "longitude" in out.coords


# ── NLDAS URL / precip conversion ────────────────────────────────────

def test_nldas_url_structure():
    # 2015-06-01 is day-of-year 152.
    url = nldas_url(2015, 152, "20150601", 13)
    assert url.endswith("/2015/152/NLDAS_FORA0125_H.A20150601.1300.020.nc")


def test_nldas_precip_accumulation_to_flux():
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {"Rainf": (("time", "lat", "lon"), np.full(shape, 3.6))},  # kg/m2 over 1 h
        coords={"time": [0], "lat": [30.0, 30.125], "lon": [-90.0, -89.875]},
    )
    out = harmonize(ds, NLDAS_MAPPINGS, lat_name="lat", lon_name="lon")
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(0.001, rel=1e-6)  # 3.6/3600
