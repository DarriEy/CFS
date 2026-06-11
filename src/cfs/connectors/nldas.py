# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NLDAS-2 connector — North American Land Data Assimilation System via OPeNDAP.

NLDAS-2 hourly forcing (NLDAS_FORA0125_H.2.0) at 0.125° over CONUS. Each hourly
timestep is a separate GES DISC NetCDF; this connector issues **one combined
OPeNDAP constraint request per hour-file** (``…nc4?Tair[0][lat][lon],…,time[0],
lat[…],lon[…]``) covering all requested variables plus coordinates, downloads
the server-cropped NetCDF4 response over the Earthdata-authenticated session,
and concatenates the hours. The hyperslab indices come from the fixed NLDAS
0.125° grid (lat 25.0625…52.9375 × lon −124.9375…−67.0625), padded outward one
cell and then trimmed locally to the exact bbox by :mod:`cfs.subset.bbox`.
All fields are canonical SI except precipitation (``Rainf``), an hourly
accumulation in kg m⁻² → flux via ``/3600``.

(Previously each hour was opened lazily via pydap and subset per variable —
~10 HTTP round-trips per hour-file; the combined constraint request is a single
round-trip per hour, the same URL form the native SYMFLUENCE NLDAS handler
uses against this server.)

Per-hour file granularity still means one HTTP request per hour (bounded by
``CFS_FETCH_CONCURRENCY``) — fine for typical windows, noted in the
FetchResult warnings for long ones. Auth-gated; covered by offline tests
(URL/index building, mocked-transport fetch, precip conversion) plus a
``network``-marked live test.
"""

from __future__ import annotations

import io
import math
import time
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.earthdata import EarthdataAuthMixin
from cfs.core.config import get_settings
from cfs.core.exceptions import ConnectorError, SubsetError
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


# Fixed NLDAS-2 0.125° CONUS grid (cell centers; probe-confirmed, regular
# ascending lat / signed lon) — lets us compute hyperslab indices without an
# extra metadata round-trip per file.
_LAT0, _NLAT = 25.0625, 224  # 25.0625 … 52.9375
_LON0, _NLON = -124.9375, 464  # -124.9375 … -67.0625
_STEP = 0.125


def _grid_indices(bbox: BoundingBox) -> tuple[tuple[int, int], tuple[int, int]]:
    """Inclusive (start, end) NLDAS grid index ranges covering ``bbox``.

    Floor/ceil pads outward by up to one cell; the local exact-bbox subset
    trims it after download.
    """
    lat_s = max(0, math.floor((bbox.min_lat - _LAT0) / _STEP))
    lat_e = min(_NLAT - 1, math.ceil((bbox.max_lat - _LAT0) / _STEP))
    lon_s = max(0, math.floor((bbox.min_lon - _LON0) / _STEP))
    lon_e = min(_NLON - 1, math.ceil((bbox.max_lon - _LON0) / _STEP))
    if lat_s > lat_e or lon_s > lon_e:
        raise SubsetError(
            f"bbox {bbox} does not overlap the NLDAS-2 CONUS grid "
            f"({_LAT0}..{_LAT0 + (_NLAT - 1) * _STEP}N, {_LON0}..{_LON0 + (_NLON - 1) * _STEP}E)"
        )
    return (lat_s, lat_e), (lon_s, lon_e)


def _subset_url(
    base_url: str,
    variables: list[str],
    lat_idx: tuple[int, int],
    lon_idx: tuple[int, int],
) -> str:
    """Combined OPeNDAP constraint URL: all variables + coords in ONE request.

    ``.nc4?`` asks Hyrax for a NetCDF4 response cropped server-side. Each
    hour-file holds a single timestep → time index ``[0]``.
    """
    lat_s, lat_e = lat_idx
    lon_s, lon_e = lon_idx
    constraints = [f"{v}[0][{lat_s}:{lat_e}][{lon_s}:{lon_e}]" for v in variables]
    constraints += ["time[0]", f"lat[{lat_s}:{lat_e}]", f"lon[{lon_s}:{lon_e}]"]
    return f"{base_url}.nc4?{','.join(constraints)}"


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

    def _fetch_subset_bytes(self, url: str) -> bytes:
        """GET one combined-constraint ``.nc4`` response (single round-trip)."""
        session = self._earthdata_session()
        resp = session.get(url, timeout=get_settings().provider_timeout_s)
        if "text/html" in resp.headers.get("Content-Type", ""):
            raise ConnectorError(
                self.slug,
                "GES DISC returned HTML instead of data — authorize the "
                "'NASA GESDISC DATA ARCHIVE' app under URS → Applications.",
            )
        if resp.status_code != 200:
            raise ConnectorError(
                self.slug, f"HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        return resp.content

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

        wanted = [m.source_name for m in _MAPPINGS
                  if variables is None or m.canonical in set(variables)]
        if not wanted:
            raise SubsetError("None of the requested variables are offered by NLDAS")

        lat_idx, lon_idx = _grid_indices(bbox)
        hours = pd.date_range(time_range.start, time_range.end, freq="h")

        def _piece(ts):
            base = _opendap_url(ts.year, int(ts.day_of_year), ts.strftime("%Y%m%d"), ts.hour)
            raw = self._fetch_subset_bytes(_subset_url(base, wanted, lat_idx, lon_idx))
            ds = xr.open_dataset(io.BytesIO(raw), engine="h5netcdf")
            # Server crop is padded one cell outward — trim to the exact bbox.
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            return apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon").load()

        pieces = await self._gather_pieces([partial(_piece, ts) for ts in hours])

        if not pieces:
            raise SubsetError(f"No NLDAS hours in [{time_range.start}, {time_range.end}]")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                "NLDAS-2 via GES DISC OPeNDAP; one combined-constraint .nc4 request "
                "per hour-file; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                f"NLDAS issues one combined-subset request per hour ({len(hours)} requested), "
                f"up to {settings.fetch_concurrency} concurrently"
            ],
        )
