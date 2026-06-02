# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NLDAS-2 connector — North American Land Data Assimilation System via OPeNDAP.

NLDAS-2 hourly forcing (NLDAS_FORA0125_H.2.0) at 0.125° over CONUS. Each hourly
timestep is a separate GES DISC NetCDF, so this connector iterates hours, opens
each Earthdata-authenticated OPeNDAP endpoint lazily, subsets to the bbox, and
concatenates. All fields are canonical SI except precipitation (APCPsfc), an
hourly accumulation in kg m⁻² → flux via ``/3600``.

⚠ Per-hour file granularity means a long time range opens many OPeNDAP endpoints
and is slow; a warning is attached to the FetchResult. Best for short windows /
small basins. Auth-gated and not live-verified here; covered by offline tests
(URL building, mappings, precip conversion). Regular ascending lat / signed lon
grid handled by :mod:`cfs.subset.bbox`.
"""

from __future__ import annotations

import time

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

OPENDAP_BASE = "https://hydro1.gesdisc.eosdis.nasa.gov/opendap/NLDAS/NLDAS_FORA0125_H.2.0"

# NLDAS_FORA0125_H.2.0 NetCDF uses CF-style names (probe-confirmed via OPeNDAP
# .dds/.das). All SI except Rainf, an hourly accumulation in kg m-2 → /3600.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("Tair", CanonicalVar.AIR_TEMPERATURE),          # K
    VariableMapping("Qair", CanonicalVar.SPECIFIC_HUMIDITY),        # kg/kg
    VariableMapping("PSurf", CanonicalVar.SURFACE_AIR_PRESSURE),    # Pa
    VariableMapping("Wind_E", CanonicalVar.EASTWARD_WIND),          # m/s
    VariableMapping("Wind_N", CanonicalVar.NORTHWARD_WIND),         # m/s
    VariableMapping("LWdown", CanonicalVar.LONGWAVE_RADIATION_DOWN),   # W/m2
    VariableMapping("SWdown", CanonicalVar.SHORTWAVE_RADIATION_DOWN),  # W/m2
    VariableMapping(
        "Rainf", CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 3600.0, note="NLDAS hourly accumulation (kg m-2) -> flux",
    ),
]


def _opendap_url(year: int, doy: int, ymd: str, hour: int) -> str:
    return f"{OPENDAP_BASE}/{year}/{doy:03d}/NLDAS_FORA0125_H.A{ymd}.{hour:02d}00.020.nc"


@register("nldas")
class NLDASConnector(EarthdataAuthMixin, BaseForcingConnector):
    slug = "nldas"
    display_name = "NLDAS-2 (0.125°, hourly, via OPeNDAP)"
    base_url = OPENDAP_BASE
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:fora0125_h",
                provider=self.slug,
                name="NLDAS-2 hourly forcing (0.125°, CONUS)",
                description=(
                    "NLDAS-2 primary forcing (NLDAS_FORA0125_H.2.0) over CONUS via "
                    "Earthdata-authenticated OPeNDAP; one file per hour."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.125,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-125.0, min_lat=25.0, max_lon=-67.0, max_lat=53.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.OPENDAP,
                license="NASA public data (open).",
                citation="Xia et al. (2012), NLDAS-2, JGR Atmospheres 117:D03109.",
            )
        ]

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

        wanted = {m.source_name for m in _MAPPINGS
                  if variables is None or m.canonical in set(variables)}
        if not wanted:
            raise SubsetError("None of the requested variables are offered by NLDAS")

        hours = pd.date_range(time_range.start, time_range.end, freq="h")

        def _piece(ts):
            url = _opendap_url(ts.year, int(ts.day_of_year), ts.strftime("%Y%m%d"), ts.hour)
            ds = self._open_opendap(url)
            keep = [v for v in wanted if v in ds.data_vars]
            ds = ds[keep]
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            return apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")

        pieces = await self._gather_pieces([lambda ts=ts: _piece(ts) for ts in hours])

        if not pieces:
            raise SubsetError(f"No NLDAS hours in [{time_range.start}, {time_range.end}]")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="NLDAS-2 via GES DISC OPeNDAP; per-hour files concatenated; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                f"NLDAS opens one OPeNDAP endpoint per hour ({len(hours)} requested), "
                f"up to {settings.fetch_concurrency} concurrently — still slow for long ranges"
            ],
        )
