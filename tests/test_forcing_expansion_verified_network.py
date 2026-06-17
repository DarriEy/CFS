# SPDX-License-Identifier: GPL-3.0-or-later
"""Live verification for the posture-only forcing expansion tier.

The 13 national/global forcing datasets exposed in
``cfs.integrations.symfluence.DATASET_SPECS`` with ``parity=None`` (admitted by
SYMFLUENCE's posture-only forcing gate on their open/attribution licence) have
NO native pipeline to parity-grade against. "Verified" for these means a real
``fetch_sync`` round-trip returns gridded data — proven here.

These reach real upstreams, so they are ``network``-marked and deselected in CI
(``-m 'not network'``). They are the standing evidence behind each posture-only
drop-in. Forecast products (GFS/ECMWF/NAM/RAP) keep only a recent horizon, so a
CURRENT window is used; land-only products (CHIRPS) need a land bbox.

Auth-gated / unverified-here products (WFDE5/CDS, BARRA2/NCI, EM-Earth/FRDR,
CMORPH, CHIRTS, GEFS, NWM operational) are intentionally NOT in DATASET_SPECS
and not covered here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytestmark = [pytest.mark.network]

_NOW = datetime.now()
_LAND = (-100.0, 40.0, -99.0, 41.0)        # central US (Nebraska) — land, for land-only products
_RECENT = (_NOW - timedelta(days=1), _NOW + timedelta(days=1))   # forecast horizon
_HIST = (datetime(2018, 6, 1), datetime(2018, 6, 2))            # archive window

# (product, bbox|None=domain-center small box, time_range, expect_var)
_CASES = {
    "gfs:forecast_0p25":          (None, _RECENT, "air_temperature"),
    "ecmwf_opendata:ifs_0p25":    (None, _RECENT, "air_temperature"),
    "nam:awphys_fcst":            (None, _RECENT, "air_temperature"),
    "rap:awp130_fcst":            (None, _RECENT, "air_temperature"),
    "gldas:noah025_3h":           (None, _HIST, "air_temperature"),
    "fldas:noah_global_monthly":  (None, _HIST, "air_temperature"),
    "merra2:single_levels":       (None, _HIST, "air_temperature"),
    "gpm:imerg_daily":            (None, _HIST, "precipitation_flux"),
    "narr:daily":                 (None, _HIST, "air_temperature"),
    "gridmet:daily":              (None, _HIST, "precipitation_flux"),
    "nclimgrid_daily:daily":      (None, _HIST, "precipitation_flux"),
    "persiann_cdr:daily":         (None, _HIST, "precipitation_flux"),
    "chirps:daily_p05":           (_LAND, _HIST, "precipitation_flux"),
}


def _small_box(b):
    from cfs.core.models import BoundingBox
    cy = (b.min_lat + b.max_lat) / 2
    cx = (b.min_lon + b.max_lon) / 2
    return BoundingBox(min_lon=cx - 0.5, min_lat=cy - 0.5, max_lon=cx + 0.5, max_lat=cy + 0.5)


@pytest.mark.parametrize("product", list(_CASES), ids=[p.split(":")[0] for p in _CASES])
def test_posture_only_dataset_returns_gridded_data(product):
    import cfs
    from cfs.core.models import BoundingBox, TimeRange  # noqa: F401

    bbox_cfg, (start, end), var = _CASES[product]
    # Resolve a small in-domain bbox from the connector's declared product domain.
    import asyncio

    from cfs.core.registry import discover, get_connector
    discover()
    slug = product.split(":")[0]
    conn_cls = get_connector(slug)

    async def _domain_box():
        async with conn_cls() as c:
            p = next(pp for pp in await c.list_products() if pp.id == product)
            return p.bbox
    bbox = bbox_cfg if bbox_cfg is not None else None
    if bbox is None:
        bbox = _small_box(asyncio.run(_domain_box()))
    else:
        from cfs.core.models import BoundingBox as BB
        bbox = BB(min_lon=bbox[0], min_lat=bbox[1], max_lon=bbox[2], max_lat=bbox[3])

    ds, result = cfs.fetch_sync(product, bbox, TimeRange(start=start, end=end), [var])
    real = int(ds.to_array().notnull().sum())
    assert real > 0, f"{product}: no real data returned for {var} in {start.date()}..{end.date()}"
