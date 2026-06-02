# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""GLDAS connector — NASA Global Land Data Assimilation System via OPeNDAP.

GLDAS-2 Noah land-surface forcing at 0.25° over the global land surface, 3-hourly
(GLDAS_NOAH025_3H). Each 3-hour timestep is a separate GES DISC NetCDF, so this
connector iterates the 3-hourly stamps, opens each Earthdata-authenticated OPeNDAP
endpoint lazily, subsets to the bbox, concatenates, and harmonizes.

Two collections are exposed as separate products:
  * ``gldas:noah025_3h``      — GLDAS-2.1 (2000-01-01 → present, version token 021)
  * ``gldas:noah025_3h_v20``  — GLDAS-2.0 (1948-01-01 → 2014, version token 020)

Every GLDAS forcing field is already in canonical SI units (temperature K, specific
humidity kg/kg, pressure Pa, wind m/s, radiation W/m², and ``Rainf_f_tavg`` a
time-averaged precipitation **rate** in kg m⁻² s⁻¹), so all mappings are identity.

Note GLDAS ships only a **scalar wind speed** (``Wind_f_inst``) — there are no u/v
components — so wind maps to the canonical ``wind_speed``, not ``eastward/northward``.
GLDAS covers land only (ocean cells are fill); QC range-warnings over water are
expected and advisory.

⚠ Per-3h file granularity means a long time range opens many OPeNDAP endpoints
(8 per day) and is slow; a warning is attached to the FetchResult. Best for short
windows / small basins. Auth-gated (NASA Earthdata) and not live-verified here;
covered by offline tests (URL building, mappings). Regular 0.25° ascending lat /
signed lon grid handled by :mod:`cfs.subset.bbox`.
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.earthdata import EarthdataAuthMixin
from cfs.core.config import get_settings
from cfs.core.exceptions import SubsetError
from cfs.core.models import (
    BoundingBox,
    FetchResult,
    ForcingProduct,
    ProductVariable,
    Protocol,
    TemporalExtent,
    TemporalResolution,
    TimeRange,
)
from cfs.core.registry import register
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

OPENDAP_BASE = "https://hydro1.gesdisc.eosdis.nasa.gov/opendap/GLDAS"

# GLDAS Noah forcing fields are CF-style and already canonical SI → identity.
# (Probe the GES DISC .dds/.das to confirm names before live verification.)
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("Tair_f_inst", CanonicalVar.AIR_TEMPERATURE),          # K
    VariableMapping("Qair_f_inst", CanonicalVar.SPECIFIC_HUMIDITY),        # kg/kg
    VariableMapping("Psurf_f_inst", CanonicalVar.SURFACE_AIR_PRESSURE),    # Pa
    VariableMapping("Wind_f_inst", CanonicalVar.WIND_SPEED),               # m/s (scalar)
    VariableMapping("SWdown_f_tavg", CanonicalVar.SHORTWAVE_RADIATION_DOWN),  # W/m2
    VariableMapping("LWdown_f_tavg", CanonicalVar.LONGWAVE_RADIATION_DOWN),   # W/m2
    VariableMapping("Rainf_f_tavg", CanonicalVar.PRECIPITATION_FLUX),      # already kg m-2 s-1
]

# product key → (collection directory, filename version token, label, year span).
_PRODUCTS = {
    "noah025_3h": (
        "GLDAS_NOAH025_3H.2.1", "021", "GLDAS-2.1 Noah 3-hourly forcing (0.25°)", (2000, 9999),
    ),
    "noah025_3h_v20": (
        "GLDAS_NOAH025_3H.2.0", "020", "GLDAS-2.0 Noah 3-hourly forcing (0.25°)", (1948, 2014),
    ),
}
_FILE_PREFIX = "GLDAS_NOAH025_3H"


def _opendap_url(collection: str, version: str, year: int, doy: int, ymd: str, hhmm: str) -> str:
    # GLDAS is laid out by /{year}/{day-of-year}/, one file per 3-hour stamp.
    return (
        f"{OPENDAP_BASE}/{collection}/{year}/{doy:03d}/"
        f"{_FILE_PREFIX}.A{ymd}.{hhmm}.{version}.nc4"
    )


@register("gldas")
class GLDASConnector(EarthdataAuthMixin, BaseForcingConnector):
    slug = "gldas"
    display_name = "NASA GLDAS-2 Noah (0.25°, 3-hourly, global land, via OPeNDAP)"
    base_url = OPENDAP_BASE
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        products = []
        for key, (collection, _ver, label, (y0, _y1)) in _PRODUCTS.items():
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{key}",
                    provider=self.slug,
                    name=label,
                    description=(
                        f"NASA Global Land Data Assimilation System ({collection}) "
                        "3-hourly Noah land-surface forcing over the global land "
                        "surface via Earthdata-authenticated OPeNDAP; one file per "
                        "3-hour stamp. Wind is a scalar speed (no u/v components)."
                    ),
                    variables=[
                        ProductVariable(canonical=m.canonical, source_name=m.source_name)
                        for m in _MAPPINGS
                    ],
                    resolution_deg=0.25,
                    crs="EPSG:4326",
                    # GLDAS spans the global *land* surface (lat ~ -60 .. 90).
                    bbox=BoundingBox(min_lon=-180, min_lat=-60, max_lon=180, max_lat=90),
                    temporal=TemporalExtent(
                        start=datetime(y0, 1, 1),
                        resolution=TemporalResolution.THREE_HOURLY,
                    ),
                    protocol=Protocol.OPENDAP,
                    license="NASA public data (open).",
                    citation="Rodell et al. (2004), GLDAS, Bull. Amer. Meteor. Soc. 85:381-394.",
                )
            )
        return products

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[object, FetchResult]:
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        key = product_id.split(":", 1)[1]
        collection, version = _PRODUCTS[key][0], _PRODUCTS[key][1]

        wanted = {m.source_name for m in _MAPPINGS
                  if variables is None or m.canonical in set(variables)}
        if not wanted:
            raise SubsetError("None of the requested variables are offered by GLDAS")

        # GLDAS stamps sit on 3-hour boundaries (00,03,…,21Z); floor the start so
        # the requested window aligns to real files, then trim after concat.
        start3 = pd.Timestamp(time_range.start).floor("3h")
        stamps = pd.date_range(start3, time_range.end, freq="3h")

        def _piece(ts):
            url = _opendap_url(
                collection, version, ts.year, int(ts.day_of_year),
                ts.strftime("%Y%m%d"), ts.strftime("%H%M"),
            )
            ds = self._open_opendap(url)
            keep = [v for v in wanted if v in ds.data_vars]
            ds = ds[keep]
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            return apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")

        pieces = await self._gather_pieces([lambda ts=ts: _piece(ts) for ts in stamps])

        if not pieces:
            raise SubsetError(f"No GLDAS data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        ds_all = ds_all.sel(time=slice(time_range.start, time_range.end))
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No GLDAS stamps in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"GLDAS ({collection}) via GES DISC OPeNDAP; per-3h files concatenated; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                f"GLDAS opens one OPeNDAP endpoint per 3-hour stamp ({len(stamps)} requested), "
                f"up to {settings.fetch_concurrency} concurrently — slow for long ranges"
            ],
        )
