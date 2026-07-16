# SPDX-License-Identifier: MIT
"""EM-Earth offline tests: keys/URLs, mappings, sources (s3/frdr/data_dir)."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from datetime import datetime

from cfs.connectors.em_earth import _MAPPINGS, EMEarthConnector
from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize

BBOX = BoundingBox(min_lon=-105.1, min_lat=39.9, max_lon=-104.8, max_lat=40.2)


def _month_ds(var: str, value: float) -> xr.Dataset:
    return xr.Dataset(
        {var: (("time", "lat", "lon"), np.full((1, 2, 2), value))},
        coords={"time": [__import__("pandas").Timestamp("2010-06-15")],
                "lat": [40.0, 40.1], "lon": [-105.0, -104.9]},
    )


def test_registered():
    discover()
    assert "em_earth" in list_providers()


def test_config_defaults_and_knobs(tmp_path):
    c = EMEarthConnector()
    assert c.anon is True and c.variant == "deterministic"
    assert c.source == "frdr" and c.data_dir is None
    c2 = EMEarthConnector(config={"anon": False, "variant": "probabilistic"})
    assert c2.anon is False and c2.variant == "probabilistic"
    c3 = EMEarthConnector(config={"source": "frdr", "data_dir": str(tmp_path)})
    assert c3.source == "frdr" and c3.data_dir == tmp_path
    with pytest.raises(SubsetError, match="source"):
        EMEarthConnector(config={"source": "globus"})
    # FRDR mirrors the S3 keys only for the deterministic daily product.
    with pytest.raises(SubsetError, match="deterministic"):
        EMEarthConnector(config={"source": "frdr", "variant": "probabilistic"})


def test_key_structure():
    c = EMEarthConnector()
    assert c._key("tmean", 2010, 6) == (
        "emearth/nc/deterministic_raw_daily/tmean/EM_Earth_deterministic_daily_tmean_201006.nc"
    )
    cp = EMEarthConnector(config={"variant": "probabilistic"})
    assert cp._key("prcp", 1990, 12).startswith("emearth/nc/probabilistic_daily/prcp/")


def test_frdr_url_structure():
    # Layout verified live 2026-06-12: FRDR's EM_Earth_v1/ mirrors S3's nc/.
    c = EMEarthConnector(config={"source": "frdr"})
    assert c._frdr_url("prcp", 2018, 6) == (
        "https://www.frdr-dfdr.ca/repo/files/6/published/publication_542/"
        "submitted_data/EM_Earth_v1/deterministic_raw_daily/prcp/"
        "EM_Earth_deterministic_daily_prcp_201806.nc"
    )


def test_temperature_celsius_to_kelvin():
    # tmean/tdew are °C → +273.15 (this conversion is QC-protected).
    ds = xr.Dataset(
        {
            "tmean": (("time", "lat", "lon"), np.full((1, 2, 2), 15.0)),
            "tdew": (("time", "lat", "lon"), np.full((1, 2, 2), 8.0)),
        },
        coords={"time": [0], "lat": [40.0, 40.1], "lon": [-105.0, -104.9]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(288.15)
    assert float(out[CanonicalVar.DEWPOINT_TEMPERATURE].values.flat[0]) == pytest.approx(281.15)


def test_precip_mm_per_day():
    # mm/day → kg m-2 s-1; verified against the FRDR file attrs ('mm day-1').
    ds = xr.Dataset(
        {"prcp": (("time", "lat", "lon"), np.full((1, 2, 2), 8.64))},
        coords={"time": [0], "lat": [40.0, 40.1], "lon": [-105.0, -104.9]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4, rel=1e-6)


async def test_s3_fetch_path_no_precip_warning(monkeypatch):
    # The mm/day assumption is verified now — precip fetches carry no warning.
    conn = EMEarthConnector(config={"source": "s3"})

    class _FS:
        def cat_file(self, key):
            t = key.rsplit("/", 1)[-1].split("_")[4]  # var token
            v = {"prcp": 8.64, "tmean": 15.0}.get(t, 0.0)
            import io
            buf = io.BytesIO()
            _month_ds(t, v).to_netcdf(buf, engine="h5netcdf")
            return buf.getvalue()

    monkeypatch.setattr(conn, "_filesystem", lambda: _FS())
    ds, result = await conn.fetch(
        "em_earth:deterministic_daily",
        BBOX,
        TimeRange(start=datetime(2010, 6, 15), end=datetime(2010, 6, 15)),
        variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
    )
    assert not [w for w in result.warnings if "mm/day" in w or "unverified" in w]
    assert float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) == pytest.approx(288.15)
    assert float(ds[CanonicalVar.PRECIPITATION_FLUX].mean()) == pytest.approx(1e-4, rel=1e-6)


@pytest.mark.parametrize("layout", ["archive", "flat"])
async def test_data_dir_staging(tmp_path, layout):
    # Pre-staged files are picked up without any network, for both layouts:
    # <data_dir>/deterministic_raw_daily/<var>/<fname> and <data_dir>/<fname>.
    fname = "EM_Earth_deterministic_daily_tmean_201006.nc"
    dest = (tmp_path / "deterministic_raw_daily" / "tmean" / fname
            if layout == "archive" else tmp_path / fname)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _month_ds("tmean", 15.0).to_netcdf(dest, engine="h5netcdf")

    conn = EMEarthConnector(config={"data_dir": str(tmp_path)})
    conn._filesystem = None  # any S3 attempt would blow up loudly
    ds, result = await conn.fetch(
        "em_earth:deterministic_daily",
        BBOX,
        TimeRange(start=datetime(2010, 6, 15), end=datetime(2010, 6, 15)),
        variables=[CanonicalVar.AIR_TEMPERATURE],
    )
    assert float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) == pytest.approx(288.15)


async def test_frdr_download_streams_and_caches(tmp_path, monkeypatch):
    # source='frdr': missing months stream from the FRDR per-file HTTPS route
    # into the data_dir archive layout; present files are not re-fetched.
    import io as _io

    calls: list[str] = []

    class _Resp:
        status_code = 200

        def __init__(self, payload: bytes):
            self._payload = payload

        def iter_content(self, chunk_size):
            yield from (self._payload[i:i + chunk_size]
                        for i in range(0, len(self._payload), chunk_size))

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        calls.append(url)
        assert url.startswith("https://www.frdr-dfdr.ca/repo/files/6/published/")
        buf = _io.BytesIO()
        _month_ds("tmean", 15.0).to_netcdf(buf, engine="h5netcdf")
        return _Resp(buf.getvalue())

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    conn = EMEarthConnector(config={"source": "frdr", "data_dir": str(tmp_path)})
    tr = TimeRange(start=datetime(2010, 6, 15), end=datetime(2010, 6, 15))
    ds, result = await conn.fetch(
        "em_earth:deterministic_daily", BBOX, tr,
        variables=[CanonicalVar.AIR_TEMPERATURE],
    )
    assert float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) == pytest.approx(288.15)
    assert "frdr" in result.provenance
    cached = (tmp_path / "deterministic_raw_daily" / "tmean" /
              "EM_Earth_deterministic_daily_tmean_201006.nc")
    assert cached.is_file()
    assert len(calls) == 1

    # Second fetch: served from the cache, no new HTTP call.
    await conn.fetch("em_earth:deterministic_daily", BBOX, tr,
                     variables=[CanonicalVar.AIR_TEMPERATURE])
    assert len(calls) == 1


async def test_frdr_404_means_month_missing(tmp_path, monkeypatch):
    class _Resp404:
        status_code = 404

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp404())

    conn = EMEarthConnector(config={"source": "frdr", "data_dir": str(tmp_path)})
    with pytest.raises(SubsetError, match="No EM-Earth data"):
        await conn.fetch(
            "em_earth:deterministic_daily", BBOX,
            TimeRange(start=datetime(2010, 6, 15), end=datetime(2010, 6, 15)),
            variables=[CanonicalVar.AIR_TEMPERATURE],
        )
