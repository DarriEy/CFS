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
