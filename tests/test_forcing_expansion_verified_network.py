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

The forcing-expansion connectors (2026-06) are covered the same way: the
archive-capable, anonymous ``cfsv2`` / ``w5e5`` / ``livneh`` / ``terraclimate`` /
``mrms`` each get a case below, with the window that was live-verified when the
connector landed (recent for the ``cfsv2`` analysis stream and the rolling
``mrms`` archive; a fixed historical window for the others). ``terraclimate`` is
a registered connector verified here even though its monthly cadence keeps it out
of DATASET_SPECS.

Auth-gated / latest-run-only / unverified-here products (WFDE5/CDS, AgERA5/CDS,
BARRA2/NCI, EM-Earth/FRDR, CMORPH, CHIRTS, GEFS, NWM operational, and the
near-real-time ECCC HRDPS/RDPS/GEPS + DWD ICON-EU forecasts) are intentionally
NOT covered here — the CDS ones are auth-gated and the forecast feeds keep no
by-date archive, so a static parametrized case would be flaky.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytestmark = [pytest.mark.network]

_NOW = datetime.now()
_LAND = (-100.0, 40.0, -99.0, 41.0)        # central US (Nebraska) — land, for land-only products
_COLORADO = (-106.0, 39.0, -105.0, 40.0)   # land box matching the connectors' verified runs
_RECENT = (_NOW - timedelta(days=1), _NOW + timedelta(days=1))   # forecast horizon
_HIST = (datetime(2018, 6, 1), datetime(2018, 6, 2))            # archive window
# CFSv2/CDAS analysis: a day-old cycle (leads f00-f09 from the cycle at/before
# start) — bounded so only a handful of leads are fetched.
_RECENT_CFS = (_NOW - timedelta(days=1), _NOW - timedelta(days=1) + timedelta(hours=6))
# MRMS rolling S3 archive (~5.6 yr, no future): a short, settled past window.
_RECENT_MRMS = (_NOW - timedelta(days=1), _NOW - timedelta(days=1) + timedelta(hours=3))

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
    # Forcing-expansion connectors (2026-06).
    "cfsv2:cdas_flux":            (None, _RECENT_CFS, "air_temperature"),
    "w5e5:obsclim_daily":         (_COLORADO, (datetime(1979, 1, 1), datetime(1979, 1, 3)), "air_temperature"),
    "livneh:daily":               (_COLORADO, (datetime(2011, 6, 1), datetime(2011, 6, 3)), "air_temperature"),
    "terraclimate:monthly":       (_COLORADO, (datetime(2020, 6, 1), datetime(2020, 6, 15)), "air_temperature"),
    "mrms:multisensor_qpe_01h":   (None, _RECENT_MRMS, "precipitation_flux"),
}


def _small_box(b):
    from cfs.core.models import BoundingBox
    cy = (b.min_lat + b.max_lat) / 2
    cx = (b.min_lon + b.max_lon) / 2
    return BoundingBox(min_lon=cx - 0.5, min_lat=cy - 0.5, max_lon=cx + 0.5, max_lat=cy + 0.5)


@pytest.mark.parametrize("product", list(_CASES), ids=[p.split(":")[0] for p in _CASES])
def test_posture_only_dataset_returns_gridded_data(product):
    import asyncio

    import cfs
    from cfs.core.models import BoundingBox, TimeRange
    from cfs.core.registry import discover, get_connector

    bbox_cfg, (start, end), var = _CASES[product]
    discover()
    slug = product.split(":")[0]
    conn_cls = get_connector(slug)

    async def _domain_box():
        async with conn_cls() as c:
            p = next(pp for pp in await c.list_products() if pp.id == product)
            return p.bbox

    if bbox_cfg is None:
        # Small in-domain box from the connector's declared product domain.
        bbox = _small_box(asyncio.run(_domain_box()))
    else:
        bbox = BoundingBox(min_lon=bbox_cfg[0], min_lat=bbox_cfg[1],
                           max_lon=bbox_cfg[2], max_lat=bbox_cfg[3])

    ds, result = cfs.fetch_sync(product, bbox, TimeRange(start=start, end=end), [var])
    real = int(ds.to_array().notnull().sum())
    assert real > 0, f"{product}: no real data returned for {var} in {start.date()}..{end.date()}"
