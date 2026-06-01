# SPDX-License-Identifier: MIT
"""EM-Earth offline tests: key building, mappings, precip warning, config."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.em_earth import _MAPPINGS, _PRECIP_WARNING, EMEarthConnector
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "em_earth" in list_providers()


def test_config_defaults_and_knobs():
    c = EMEarthConnector()
    assert c.anon is True and c.variant == "deterministic"
    c2 = EMEarthConnector(config={"anon": False, "variant": "probabilistic"})
    assert c2.anon is False and c2.variant == "probabilistic"


def test_key_structure():
    c = EMEarthConnector()
    assert c._key("tmean", 2010, 6) == (
        "emearth/nc/deterministic_raw_daily/tmean/EM_Earth_deterministic_daily_tmean_201006.nc"
    )
    cp = EMEarthConnector(config={"variant": "probabilistic"})
    assert cp._key("prcp", 1990, 12).startswith("emearth/nc/probabilistic_daily/prcp/")


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


def test_precip_assumed_mm_per_day():
    ds = xr.Dataset(
        {"prcp": (("time", "lat", "lon"), np.full((1, 2, 2), 8.64))},
        coords={"time": [0], "lat": [40.0, 40.1], "lon": [-105.0, -104.9]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-4, rel=1e-6)


async def test_precip_request_emits_unverified_warning(monkeypatch):
    # Requesting precipitation must surface the unverified-units warning. We stub
    # the S3 read so the warning path is exercised without network.
    conn = EMEarthConnector()

    class _FS:
        def cat_file(self, key):
            t = key.rsplit("/", 1)[-1].split("_")[4]  # var token
            import numpy as np
            v = {"prcp": 8.64, "tmean": 15.0}.get(t, 0.0)
            ds = xr.Dataset(
                {t: (("time", "lat", "lon"), np.full((1, 2, 2), v))},
                coords={"time": [__import__("pandas").Timestamp("2010-06-15")],
                        "lat": [40.0, 40.1], "lon": [-105.0, -104.9]},
            )
            import io
            buf = io.BytesIO()
            ds.to_netcdf(buf, engine="h5netcdf")
            return buf.getvalue()

    monkeypatch.setattr(conn, "_filesystem", lambda: _FS())
    from datetime import datetime

    from cfs.core.models import BoundingBox, TimeRange
    ds, result = await conn.fetch(
        "em_earth:deterministic_daily",
        BoundingBox(min_lon=-105.1, min_lat=39.9, max_lon=-104.8, max_lat=40.2),
        TimeRange(start=datetime(2010, 6, 15), end=datetime(2010, 6, 15)),
        variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
    )
    assert _PRECIP_WARNING in result.warnings
    assert float(ds[CanonicalVar.AIR_TEMPERATURE].mean()) == pytest.approx(288.15)
