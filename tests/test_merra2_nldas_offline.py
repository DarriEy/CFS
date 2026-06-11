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
from cfs.connectors.nldas import _grid_indices, _subset_url
from cfs.connectors.nldas import _opendap_url as nldas_url
from cfs.core.models import BoundingBox, TimeRange
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


# ── NLDAS combined-constraint subsetting (one request per hour-file) ─

NLDAS_BBOX = BoundingBox(min_lon=-100.0, min_lat=40.0, max_lon=-99.5, max_lat=40.4)


def test_nldas_grid_indices():
    # Floor/ceil against the fixed 0.125° grid (lat0=25.0625, lon0=-124.9375),
    # padded one cell outward and clipped to the 224x464 CONUS extent.
    lat_idx, lon_idx = _grid_indices(NLDAS_BBOX)
    assert lat_idx == (119, 123)
    assert lon_idx == (199, 204)


def test_nldas_grid_indices_clipped_and_disjoint():
    conus = BoundingBox(min_lon=-130.0, min_lat=20.0, max_lon=-60.0, max_lat=60.0)
    assert _grid_indices(conus) == ((0, 223), (0, 463))
    from cfs.core.exceptions import SubsetError

    with pytest.raises(SubsetError):
        _grid_indices(BoundingBox(min_lon=10.0, min_lat=45.0, max_lon=11.0, max_lat=46.0))


def test_nldas_subset_url_single_combined_request():
    # ALL variables + coords in ONE .nc4 constraint expression — the native
    # SYMFLUENCE handler's URL form (no per-variable round-trips).
    base = nldas_url(2015, 152, "20150601", 13)
    url = _subset_url(base, [m.source_name for m in NLDAS_MAPPINGS], (119, 123), (199, 204))
    assert url.count("?") == 1 and ".nc4?" in url
    for m in NLDAS_MAPPINGS:
        assert f"{m.source_name}[0][119:123][199:204]" in url
    assert url.endswith("time[0],lat[119:123],lon[199:204]")


def _fake_nldas_response(url: str, tmp_path) -> bytes:
    """Build an in-memory NLDAS-like .nc4 response for a constraint URL."""
    import re

    import pandas as pd

    m = re.search(r"\.A(\d{8})\.(\d{2})00\.020\.nc\.nc4\?", url)
    assert m, url
    when = pd.Timestamp(m.group(1)) + pd.Timedelta(hours=int(m.group(2)))
    lats = [25.0625 + 0.125 * i for i in range(119, 124)]
    lons = [-124.9375 + 0.125 * j for j in range(199, 205)]
    shape = (1, len(lats), len(lons))
    fields = {"Tair": 290.0, "Qair": 0.008, "PSurf": 95000.0, "Wind_E": 2.0,
              "Wind_N": -1.0, "LWdown": 350.0, "SWdown": 500.0, "Rainf": 0.36}
    ds = xr.Dataset(
        {k: (("time", "lat", "lon"), np.full(shape, v)) for k, v in fields.items()},
        coords={"time": [when], "lat": lats, "lon": lons},
    )
    path = tmp_path / f"resp_{m.group(1)}_{m.group(2)}.nc4"
    ds.to_netcdf(path, engine="h5netcdf")
    return path.read_bytes()


async def test_nldas_fetch_one_request_per_hour(monkeypatch, tmp_path):
    pytest.importorskip("h5netcdf")
    from datetime import datetime

    from cfs.connectors.nldas import NLDASConnector

    seen: list[str] = []

    def fake_fetch(self, url: str) -> bytes:
        seen.append(url)
        return _fake_nldas_response(url, tmp_path)

    monkeypatch.setattr(NLDASConnector, "_fetch_subset_bytes", fake_fetch)
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 2))
    ds, r = await NLDASConnector().fetch("nldas:fora0125_h", NLDAS_BBOX, tr)

    # One combined request per hour-file — not per variable.
    assert len(seen) == 3
    for url in seen:
        assert url.count("?") == 1 and ".nc4?" in url
        for m in NLDAS_MAPPINGS:
            assert m.source_name in url
    assert r.n_times == 3
    # Padded server crop trimmed to the exact bbox (3 lat x 4 lon cells).
    assert r.n_lat == 3 and r.n_lon == 4
    assert float(ds[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(290.0)
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4)


async def test_nldas_fetch_requested_variables_only(monkeypatch, tmp_path):
    pytest.importorskip("h5netcdf")
    from datetime import datetime

    from cfs.connectors.nldas import NLDASConnector

    seen: list[str] = []

    def fake_fetch(self, url: str) -> bytes:
        seen.append(url)
        return _fake_nldas_response(url, tmp_path)

    monkeypatch.setattr(NLDASConnector, "_fetch_subset_bytes", fake_fetch)
    tr = TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 0))
    ds, r = await NLDASConnector().fetch(
        "nldas:fora0125_h", NLDAS_BBOX, tr, variables=[CanonicalVar.AIR_TEMPERATURE]
    )
    assert len(seen) == 1 and "Tair[0]" in seen[0] and "Rainf" not in seen[0]
    assert list(ds.data_vars) == [CanonicalVar.AIR_TEMPERATURE]


def test_nldas_precip_accumulation_to_flux():
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {"Rainf": (("time", "lat", "lon"), np.full(shape, 3.6))},  # kg/m2 over 1 h
        coords={"time": [0], "lat": [30.0, 30.125], "lon": [-90.0, -89.875]},
    )
    out = harmonize(ds, NLDAS_MAPPINGS, lat_name="lat", lon_name="lon")
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(0.001, rel=1e-6)  # 3.6/3600
