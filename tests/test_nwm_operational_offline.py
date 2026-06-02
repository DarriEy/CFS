# SPDX-License-Identifier: MIT
"""NWM Operational offline tests: products, mappings, harmonization."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.nwm_operational import _MAPPINGS, NWMOperationalConnector
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "nwm_operational" in list_providers()


async def test_products():
    conn = NWMOperationalConnector()
    products = await conn.list_products()
    ids = {p.id for p in products}
    # Only analysis_assim is exposed (maps cleanly to a valid-time forcing via the
    # tm00 file); the short/medium-range forecasts need a cycle×lead resolver and
    # are intentionally out of scope.
    assert ids == {"nwm_operational:analysis_assim"}


def test_mappings_identity():
    # NWM forcing is native SI.
    assert all(m.scale == 1.0 and not m.deaccumulate for m in _MAPPINGS)
    canon = {m.canonical for m in _MAPPINGS}
    assert {CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.AIR_TEMPERATURE,
            CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.SURFACE_AIR_PRESSURE} <= canon


def test_harmonization_2d_lcc():
    # LDASIN files have 2-D lat/lon and y/x dims.
    shape = (1, 2, 2)
    lat2d = np.array([[40.0, 40.01], [40.02, 40.03]])
    lon2d = np.array([[-105.0, -104.99], [-104.98, -104.97]])
    ds = xr.Dataset(
        {
            "T2D": (("time", "y", "x"), np.full(shape, 290.0)),
            "RAINRATE": (("time", "y", "x"), np.full(shape, 1e-5)),
        },
        coords={
            "time": [0],
            "lat": (("y", "x"), lat2d),
            "lon": (("y", "x"), lon2d),
        },
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    assert float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0]) == pytest.approx(1e-5)
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(290.0)
    assert out["latitude"].dims == ("y", "x")
