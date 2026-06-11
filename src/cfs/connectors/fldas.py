# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""FLDAS connector — NASA FEWS NET Land Data Assimilation System via OPeNDAP.

FLDAS is GLDAS's sibling: the same NASA LDAS framework and the same GES DISC
Earthdata-authenticated OPeNDAP store, run for famine-early-warning over Africa
and globally. This connector exposes the **monthly** global Noah product at 0.1°:

  * ``fldas:noah_global_monthly`` — ``FLDAS_NOAH01_C_GL_M.001`` (1982-01 → present)

Each monthly NetCDF is a single ``time=1`` timestep, so this connector iterates the
month stamps, opens each Earthdata-authenticated OPeNDAP endpoint lazily, subsets to
the bbox, concatenates along time, and harmonizes.

Every FLDAS Noah forcing field is already in canonical SI units (temperature K,
specific humidity kg/kg, pressure Pa, wind m/s, radiation W/m², and ``Rainf_f_tavg``
a time-averaged precipitation **rate** in kg m⁻² s⁻¹), so all mappings are identity.

Like GLDAS, FLDAS ships only a **scalar wind speed** (``Wind_f_tavg``) — no u/v
components — so wind maps to the canonical ``wind_speed``, not ``eastward/northward``.
FLDAS covers land only (ocean cells are fill); QC range-warnings over water are
expected and advisory.

⚠ Only a *monthly* product is published on the GES DISC FLDAS Hyrax (no daily
collection exists there as of 2026), so this is a coarse temporal resolution — best
for climatology/seasonal forcing, not event-scale hydrology. The grid coordinate
names are ``X`` (longitude) / ``Y`` (latitude); the regular 0.1° ascending-lat /
signed-lon grid is handled by :mod:`cfs.subset.bbox`. Live-verified against the GES
DISC store over an East Africa bbox.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

OPENDAP_BASE = "https://hydro1.gesdisc.eosdis.nasa.gov/opendap/FLDAS"

# FLDAS grid coordinate names (confirmed from the live .das/.dds: X=lon, Y=lat).
LAT_NAME = "Y"
LON_NAME = "X"

# FLDAS Noah forcing fields are CF-style and already canonical SI → identity.
# (Confirmed against the live GES DISC FLDAS_NOAH01_C_GL_M.001 .das.)
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("Tair_f_tavg", CanonicalVar.AIR_TEMPERATURE),          # K
    VariableMapping("Qair_f_tavg", CanonicalVar.SPECIFIC_HUMIDITY),        # kg/kg
    VariableMapping("Psurf_f_tavg", CanonicalVar.SURFACE_AIR_PRESSURE),    # Pa
    VariableMapping("Wind_f_tavg", CanonicalVar.WIND_SPEED),               # m/s (scalar)
    VariableMapping("SWdown_f_tavg", CanonicalVar.SHORTWAVE_RADIATION_DOWN),  # W/m2
    VariableMapping("LWdown_f_tavg", CanonicalVar.LONGWAVE_RADIATION_DOWN),   # W/m2
    VariableMapping("Rainf_f_tavg", CanonicalVar.PRECIPITATION_FLUX),      # already kg m-2 s-1
]

# product key → (collection directory, filename prefix, label, year span).
_PRODUCTS = {
    "noah_global_monthly": (
        "FLDAS_NOAH01_C_GL_M.001",
        "FLDAS_NOAH01_C_GL_M",
        "FLDAS Noah global monthly forcing (0.1°)",
        (1982, 9999),
    ),
}


def _opendap_url(collection: str, prefix: str, year: int, month: int) -> str:
    # FLDAS monthly is laid out by /{year}/, one file per month (time=1).
    return f"{OPENDAP_BASE}/{collection}/{year}/{prefix}.A{year:04d}{month:02d}.001.nc"


@register("fldas")
class FLDASConnector(EarthdataAuthMixin, BaseForcingConnector):
    slug = "fldas"
    display_name = "NASA FLDAS Noah (0.1°, monthly, global land, via OPeNDAP)"
    base_url = OPENDAP_BASE
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        products = []
        for key, (collection, _prefix, label, (y0, _y1)) in _PRODUCTS.items():
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{key}",
                    provider=self.slug,
                    name=label,
                    description=(
                        f"NASA FEWS NET Land Data Assimilation System ({collection}) "
                        "monthly Noah land-surface forcing at 0.1° over the global land "
                        "surface via Earthdata-authenticated OPeNDAP; one file per month. "
                        "Wind is a scalar speed (no u/v components)."
                    ),
                    variables=[
                        ProductVariable(canonical=m.canonical, source_name=m.source_name)
                        for m in _MAPPINGS
                    ],
                    resolution_deg=0.1,
                    crs="EPSG:4326",
                    # FLDAS spans the global *land* surface (lat ~ -60 .. 90).
                    bbox=BoundingBox(min_lon=-180, min_lat=-60, max_lon=180, max_lat=90),
                    temporal=TemporalExtent(
                        start=datetime(y0, 1, 1),
                        resolution=TemporalResolution.MONTHLY,
                    ),
                    protocol=Protocol.OPENDAP,
                    license="NASA public data (open).",
                    citation="McNally et al. (2017), FLDAS, Scientific Data 4:170012.",
                )
            )
        return products

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        key = product_id.split(":", 1)[1]
        collection, prefix = _PRODUCTS[key][0], _PRODUCTS[key][1]

        wanted = {m.source_name for m in _MAPPINGS
                  if variables is None or m.canonical in set(variables)}
        if not wanted:
            raise SubsetError("None of the requested variables are offered by FLDAS")

        # FLDAS files are stamped on month starts; floor the start to the month so
        # the requested window aligns to real files, then trim after concat.
        start_m = pd.Timestamp(time_range.start).to_period("M").to_timestamp()
        stamps = pd.date_range(start_m, time_range.end, freq="MS")

        def _piece(ts):
            url = _opendap_url(collection, prefix, ts.year, ts.month)
            ds = self._open_opendap(url)
            keep = [v for v in wanted if v in ds.data_vars]
            ds = ds[keep]
            plan = plan_bbox_subset(ds, bbox, lat_name=LAT_NAME, lon_name=LON_NAME)
            return apply_bbox_subset(ds, plan, lat_name=LAT_NAME, lon_name=LON_NAME)

        pieces = await self._gather_pieces([partial(_piece, ts) for ts in stamps])

        if not pieces:
            raise SubsetError(f"No FLDAS data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        ds_all = ds_all.sel(time=slice(time_range.start, time_range.end))
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No FLDAS months in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name=LAT_NAME, lon_name=LON_NAME)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"FLDAS ({collection}) via GES DISC OPeNDAP; per-month files concatenated; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                "FLDAS is monthly-resolution (coarse) — best for climatology/seasonal "
                "forcing, not event-scale hydrology"
            ],
        )
