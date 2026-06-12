# SPDX-License-Identifier: MIT
"""RDRS / CONUS404 offline tests: 2-D harmonization, de-accumulation, mappings."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.conus404 import _MAPPINGS as C404_MAPPINGS
from cfs.connectors.rdrs import _MAPPINGS as RDRS_MAPPINGS
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert {"rdrs", "conus404"} <= set(list_providers())


def _grid2d_cube(data_vars, ny=2, nx=3, nt=3):
    """A cube with 2-D lat/lon over (ydim, xdim), like RDRS/CONUS404."""
    lat2d = np.linspace(50, 51, ny * nx).reshape(ny, nx)
    lon2d = np.linspace(-115, -113, ny * nx).reshape(ny, nx)
    return xr.Dataset(
        {k: (("time", "yy", "xx"), v) for k, v in data_vars.items()},
        coords={
            "lat": (("yy", "xx"), lat2d),
            "lon": (("yy", "xx"), lon2d),
            "time": np.arange(nt),
        },
    )


# ── RDRS: all identity, 2-D coords renamed ───────────────────────────

def test_rdrs_identity_and_coord_rename():
    nt, ny, nx = 3, 2, 3
    ds = _grid2d_cube(
        {
            "tas": np.full((nt, ny, nx), 283.0),
            "pr": np.full((nt, ny, nx), 5e-5),
            "huss": np.full((nt, ny, nx), 0.004),
        },
        ny, nx, nt,
    )
    out = harmonize(ds, RDRS_MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(283.0)
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(5e-5)
    assert float(out[CanonicalVar.SPECIFIC_HUMIDITY].values.flat[0]) == pytest.approx(0.004)
    # 2-D lat/lon coords standardized to canonical names.
    assert "latitude" in out.coords and "longitude" in out.coords
    assert out["latitude"].dims == ("yy", "xx")  # still 2-D over native dims


def test_rdrs_all_identity():
    assert all(m.scale == 1.0 and not m.deaccumulate for m in RDRS_MAPPINGS)


# ── CONUS404: radiation de-accumulated, precip linear ────────────────

def test_conus404_radiation_deaccumulated_precip_linear():
    nt, ny, nx = 3, 2, 3
    # ACSWDNB accumulates 1.8e6, 3.6e6, 5.4e6 J/m2 → increments 1.8e6 each → /3600 = 500 W/m2
    acsw = np.stack([np.full((ny, nx), v) for v in (1.8e6, 3.6e6, 5.4e6)])
    # PREC_ACC_NC is per-hour buckets (independent): 3.6, 7.2, 3.6 mm → /3600 each
    prec = np.stack([np.full((ny, nx), v) for v in (3.6, 7.2, 3.6)])
    ds = _grid2d_cube({"ACSWDNB": acsw, "PREC_ACC_NC": prec}, ny, nx, nt)

    out = harmonize(ds, C404_MAPPINGS, lat_name="lat", lon_name="lon")
    sw = out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].values[:, 0, 0]
    assert sw[1] == pytest.approx(500.0, rel=1e-6)
    assert sw[2] == pytest.approx(500.0, rel=1e-6)
    # Precip is linear per-bucket, NOT differenced: 7.2 mm/h → 0.002 kg m-2 s-1.
    pr = out[CanonicalVar.PRECIPITATION_FLUX].values[:, 0, 0]
    assert pr[1] == pytest.approx(7.2 / 3600.0, rel=1e-6)
    assert pr[2] == pytest.approx(3.6 / 3600.0, rel=1e-6)


def test_conus404_only_radiation_deaccumulates():
    deaccum = {m.source_name for m in C404_MAPPINGS if m.deaccumulate}
    assert deaccum == {"ACSWDNB", "ACLWDNB"}


# ── RDRS fetch: time pushdown + single upstream read per variable ─────
#
# The PAVICS NCML spans 45 years; the perf defect was opening it with
# chunks={} (whole-variable dask chunks), which kept the time slice out of
# the DAP constraint (full time axis pulled per variable) and re-pulled
# everything once for QC and again on .load(). These tests spy on the raw
# backend reads to pin the fixed contract: the time window is part of every
# upstream read, and each variable is read exactly once.

import asyncio  # noqa: E402
from datetime import datetime  # noqa: E402

from xarray.backends import BackendArray  # noqa: E402
from xarray.core import indexing  # noqa: E402

from cfs.connectors.rdrs import RDRSConnector  # noqa: E402
from cfs.core.models import BoundingBox, TimeRange  # noqa: E402

_NT, _NY, _NX = 96, 12, 14  # hourly from 2018-06-01 over a small 2-D grid


class _SpyBackendArray(BackendArray):
    """Lazy backend array that logs every raw upstream read (name, index key)."""

    def __init__(self, name, data, log):
        self._name = name
        self._data = data
        self._log = log
        self.shape = data.shape
        self.dtype = data.dtype

    def __getitem__(self, key):
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.BASIC, self._raw_read
        )

    def _raw_read(self, key):
        self._log.append((self._name, key))
        return self._data[key]


def _spy_store():
    """An in-memory stand-in for the PAVICS aggregation with read accounting."""
    log: list[tuple[str, tuple]] = []
    lon2d, lat2d = np.meshgrid(np.linspace(-113.0, -111.0, _NX), np.linspace(41.0, 43.0, _NY))
    fills = {"tas": 283.0, "pr": 5e-5, "huss": 0.004}  # plausible → QC stays quiet
    data_vars = {
        name: xr.Variable(
            ("time", "rlat", "rlon"),
            indexing.LazilyIndexedArray(
                _SpyBackendArray(name, np.full((_NT, _NY, _NX), fill, dtype="float32"), log)
            ),
        )
        for name, fill in fills.items()
    }
    ds = xr.Dataset(
        data_vars,
        coords={
            "lat": (("rlat", "rlon"), lat2d),
            "lon": (("rlat", "rlon"), lon2d),
            "time": np.datetime64("2018-06-01", "ns") + np.arange(_NT) * np.timedelta64(1, "h"),
        },
    )
    return ds, log


def _slice_len(sl: slice, n: int) -> int:
    return len(range(*sl.indices(n)))


def test_rdrs_fetch_pushes_time_window_down_and_reads_upstream_once(monkeypatch):
    store, log = _spy_store()
    monkeypatch.setattr(RDRSConnector, "_open", staticmethod(lambda url: store))

    bbox = BoundingBox(min_lon=-112.4, min_lat=41.4, max_lon=-111.6, max_lat=42.4)
    tr = TimeRange(start=datetime(2018, 6, 2), end=datetime(2018, 6, 3))  # 25 hourly steps
    out, result = asyncio.run(RDRSConnector().fetch("rdrs:casr_v32", bbox, tr))

    assert out.sizes["time"] == 25 and result.n_times == 25
    # Exactly ONE raw read per variable: QC sampling and any caller-side
    # .load() must hit the already-materialized data, not re-pull upstream.
    reads_per_var = {name: sum(1 for n, _ in log if n == name) for name in ("tas", "pr", "huss")}
    assert reads_per_var == {"tas": 1, "pr": 1, "huss": 1}
    # Every raw read carries the time window AND the spatial window — never
    # the full time axis (the chunks={} regression) or the full grid.
    for name, key in log:
        t_sl, y_sl, x_sl = key
        assert _slice_len(t_sl, _NT) == 25, f"{name}: time not pushed down: {t_sl}"
        assert _slice_len(y_sl, _NY) < _NY and _slice_len(x_sl, _NX) < _NX, f"{name}: {key}"
    # Returned cube is materialized (lazy=False is true) and QC found nothing.
    assert result.lazy is False
    assert isinstance(out[CanonicalVar.AIR_TEMPERATURE].data, np.ndarray)
    assert result.warnings == []
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(5e-5)


def test_rdrs_open_is_dask_free(monkeypatch):
    """_open must NOT pass chunks= — dask single-chunk arrays defeat the DAP
    time-slice pushdown on the 45-year aggregation."""
    captured: dict = {}

    def fake_open_dataset(url, **kwargs):
        captured.update(kwargs, url=url)
        return "sentinel"

    monkeypatch.setattr(xr, "open_dataset", fake_open_dataset)
    assert RDRSConnector._open("https://example.invalid/agg.ncml") == "sentinel"
    assert captured["engine"] == "netcdf4"
    assert "chunks" not in captured
