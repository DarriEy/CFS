# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""GPM IMERG connector — NASA satellite precipitation via authenticated OPeNDAP.

IMERG Final-run daily precipitation (GPM_3IMERGDF.07), 0.1° quasi-global, one
GES DISC NetCDF per day. Opened lazily through the Earthdata-authenticated
OPeNDAP endpoint, subset to bbox + day, and harmonized: daily ``precipitation``
in mm/day → canonical ``precipitation_flux`` (kg m⁻² s⁻¹) via ``/86400``.

Two IMERG quirks are handled here:
  * the precip variable name varies (``precipitation`` in V07, ``precipitationCal``
    in older runs) — detected and normalized;
  * arrays may be dimensioned ``(time, lon, lat)`` (or lack a ``time`` dim on a
    single-day file) — subset by *label* and normalized to ``(time, latitude,
    longitude)``.

Only the Final daily run is exposed: the Late/Early runs use different filename
tokens and the Monthly product uses different units (mm/hr) — both deferred to
avoid shipping an unverified conversion. Auth-gated; offline-tested only.
"""

from __future__ import annotations

import time
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

GES_DISC = "https://gpm1.gesdisc.eosdis.nasa.gov"
_OPENDAP_BASE = "/opendap/GPM_L3"
_VERSION = "V07B"
SECONDS_PER_DAY = 86400.0

# product-id suffix → (collection, daily-file prefix). Final is the gauge-corrected
# run; Early/Late are near-real-time. All confirmed live on GES DISC (HTTP 200).
_RUNS = {
    "imerg_daily": ("GPM_3IMERGDF.07", "3B-DAY.MS.MRG.3IMERG"),
    "imerg_early": ("GPM_3IMERGDE.07", "3B-DAY-E.MS.MRG.3IMERG"),
    "imerg_late": ("GPM_3IMERGDL.07", "3B-DAY-L.MS.MRG.3IMERG"),
}

# After detection the precip field is renamed to this internal name, then mapped.
_PRECIP = "precipitation"
_PRECIP_CANDIDATES = ("precipitation", "precipitationCal", "precipitationUncal")

_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        _PRECIP, CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / SECONDS_PER_DAY, note="IMERG daily total (mm/day) -> flux (kg m-2 s-1)",
    ),
]


def _opendap_url(product_id: str, year: int, month: int, ymd: str) -> str:
    # IMERG *daily* is laid out by /{year}/{month}/ (NOT day-of-year, which is the
    # half-hourly product's layout) — confirmed against GES DISC. The run (Final/
    # Early/Late) selects the collection and file prefix.
    collection, prefix = _RUNS[product_id.split(":", 1)[1]]
    return (
        f"{GES_DISC}{_OPENDAP_BASE}/{collection}/{year}/{month:02d}/"
        f"{prefix}.{ymd}-S000000-E235959.{_VERSION}.nc4"
    )


def _detect_precip(ds) -> str | None:
    """Return the IMERG precipitation variable name, or None if absent."""
    for cand in _PRECIP_CANDIDATES:
        if cand in ds.data_vars:
            return cand
    matches = [v for v in ds.data_vars if "precip" in str(v).lower()]
    return matches[0] if matches else None


@register("gpm")
class GPMConnector(EarthdataAuthMixin, BaseForcingConnector):
    slug = "gpm"
    display_name = "GPM IMERG Final daily precipitation (0.1°, via OPeNDAP)"
    base_url = GES_DISC
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:imerg_daily",
                provider=self.slug,
                name="GPM IMERG Final daily precipitation (0.1°)",
                description=(
                    "NASA GPM Integrated Multi-satellitE Retrievals (IMERG) Final "
                    "run, daily 0.1° precipitation via Earthdata-authenticated OPeNDAP."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="NASA public data (open).",
                citation="Huffman et al. (2023), GPM IMERG V07, NASA GES DISC.",
            ),
            ForcingProduct(
                id=f"{self.slug}:imerg_early",
                provider=self.slug,
                name="GPM IMERG Early daily precipitation (0.1°)",
                description=(
                    "NASA GPM IMERG Early run (near-real-time), daily 0.1° "
                    "precipitation via Earthdata-authenticated OPeNDAP."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="NASA public data (open).",
                citation="Huffman et al. (2023), GPM IMERG V07, NASA GES DISC.",
            ),
            ForcingProduct(
                id=f"{self.slug}:imerg_late",
                provider=self.slug,
                name="GPM IMERG Late daily precipitation (0.1°)",
                description=(
                    "NASA GPM IMERG Late run (near-real-time), daily 0.1° "
                    "precipitation via Earthdata-authenticated OPeNDAP."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="NASA public data (open).",
                citation="Huffman et al. (2023), GPM IMERG V07, NASA GES DISC.",
            ),
        ]

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

        if variables is not None and CanonicalVar.PRECIPITATION_FLUX not in set(variables):
            raise SubsetError("GPM offers only precipitation_flux")

        days = pd.date_range(time_range.start.date(), time_range.end.date(), freq="D")

        def _piece(d):
            url = _opendap_url(product_id, d.year, d.month, d.strftime("%Y%m%d"))
            ds = self._open_opendap(url)
            var = _detect_precip(ds)
            if var is None:
                return None
            ds = ds[[var]].rename({var: _PRECIP})
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            if "time" not in ds.dims:
                ds = ds.expand_dims(time=[pd.Timestamp(d)])
            return ds

        pieces = await self._gather_pieces([partial(_piece, d) for d in days])

        if not pieces:
            raise SubsetError(
                f"No GPM IMERG data in [{time_range.start}, {time_range.end}] for the bbox"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        # Normalize orientation (IMERG can be time, lon, lat).
        canonical = canonical.transpose("time", "latitude", "longitude", ...)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="GPM IMERG Final daily via GES DISC OPeNDAP; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
