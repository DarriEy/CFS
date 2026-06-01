# SPDX-License-Identifier: MIT
"""MSWEP offline tests: path enumeration, conversions, config, rclone errors."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.mswep import RESOLUTIONS, MSWEPConnector, _detect_precip
from cfs.core.models import TimeRange
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import VariableMapping, harmonize


def test_registered():
    discover()
    assert "mswep" in list_providers()


async def test_products_per_resolution():
    ids = {p.id for p in await MSWEPConnector().list_products()}
    assert ids == {"mswep:daily", "mswep:3hourly"}


def test_version_and_remote_config():
    assert MSWEPConnector().version == "V300"
    assert MSWEPConnector().remote == "GoogleDrive"
    c = MSWEPConnector(config={"version": "V280", "remote": "MyDrive"})
    assert c.version == "V280" and c.remote == "MyDrive"


def test_remote_from_env(monkeypatch):
    monkeypatch.setenv("MSWEP_RCLONE_REMOTE", "EnvDrive")
    assert MSWEPConnector().remote == "EnvDrive"


# ── Remote path enumeration (day-of-year layout) ─────────────────────

def test_daily_paths():
    conn = MSWEPConnector(config={"version": "V300"})
    tr = TimeRange(start=datetime(2015, 1, 1), end=datetime(2015, 1, 2))
    paths = [rel for _ts, rel in conn._relative_paths("daily", tr)]
    assert paths == ["MSWEP_V300/Daily/2015/001.nc", "MSWEP_V300/Daily/2015/002.nc"]


def test_3hourly_paths_include_hour():
    conn = MSWEPConnector(config={"version": "V280"})
    tr = TimeRange(start=datetime(2015, 1, 1, 0), end=datetime(2015, 1, 1, 6))
    paths = [rel for _ts, rel in conn._relative_paths("3hourly", tr)]
    assert paths == [
        "MSWEP_V280/3hourly/2015/00100.nc",
        "MSWEP_V280/3hourly/2015/00103.nc",
        "MSWEP_V280/3hourly/2015/00106.nc",
    ]


# ── Unit conversions per resolution ──────────────────────────────────

@pytest.mark.parametrize(
    "resolution,value,expected",
    [("daily", 8.64, 1e-4), ("3hourly", 10.8, 1e-3)],  # mm/step / seconds
)
def test_precip_conversion(resolution, value, expected):
    seconds = RESOLUTIONS[resolution][1]
    ds = xr.Dataset(
        {"precipitation": (("time", "lat", "lon"), np.full((1, 2, 2), value))},
        coords={"time": [0], "lat": [0.0, 0.1], "lon": [30.0, 30.1]},
    )
    mapping = [VariableMapping("precipitation", CanonicalVar.PRECIPITATION_FLUX, scale=1.0 / seconds)]
    out = harmonize(ds, mapping, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(expected, rel=1e-6)


def test_detect_precip_variants():
    ds_p = xr.Dataset({"precipitation": (("lat",), [1.0])}, coords={"lat": [0.0]})
    ds_q = xr.Dataset({"precip": (("lat",), [1.0])}, coords={"lat": [0.0]})
    assert _detect_precip(ds_p) == "precipitation"
    assert _detect_precip(ds_q) == "precip"


def test_missing_rclone_raises_clear_error(monkeypatch):
    import cfs.connectors.protocols.rclone as rc
    from cfs.core.exceptions import RegistrationRequiredError

    monkeypatch.setattr(rc.shutil, "which", lambda _: None)
    with pytest.raises(RegistrationRequiredError):
        MSWEPConnector()._rclone_bin()
