# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""MERRA-2 connector — NASA MERRA-2 reanalysis via authenticated OPeNDAP.

MERRA-2 surface forcing is spread across three GES DISC collections (single-level
diagnostics, radiation, surface flux), each a daily NetCDF with 24 hourly steps.
This connector opens the daily OPeNDAP endpoints lazily (Earthdata-authenticated),
subsets each to the bbox + day, merges the collections, concatenates the days,
and harmonizes. Every MERRA-2 field is already in canonical SI units (temperature
K, pressure Pa, specific humidity kg/kg, winds m/s, radiation W/m², precipitation
kg m⁻² s⁻¹), so all mappings are identity.

Auth-gated (NASA Earthdata) and not live-verified here; covered by offline tests
(URL/stream building, mappings). Regular 0.5°×0.625° grid, latitude descending —
handled by :mod:`cfs.subset.bbox`.
"""

from __future__ import annotations

import time
from datetime import timedelta
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

OPENDAP_BASE = "https://goldsmr4.gesdisc.eosdis.nasa.gov/opendap/MERRA2"

# Collection key → (collection id, short name, native variables present).
_COLLECTIONS = {
    "slv": ("M2T1NXSLV.5.12.4", "tavg1_2d_slv_Nx", ["T2M", "PS", "QV2M", "U10M", "V10M"]),
    "rad": ("M2T1NXRAD.5.12.4", "tavg1_2d_rad_Nx", ["SWGDN", "LWGAB"]),
    "flx": ("M2T1NXFLX.5.12.4", "tavg1_2d_flx_Nx", ["PRECTOTCORR"]),
}

# MERRA-2 file-stream number by year range.
_STREAM_MAP = [(1980, 1991, 100), (1992, 2000, 200), (2001, 2010, 300), (2011, 9999, 400)]

# All native fields are already canonical SI → identity mappings.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("T2M", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("PS", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping("QV2M", CanonicalVar.SPECIFIC_HUMIDITY),
    VariableMapping("U10M", CanonicalVar.EASTWARD_WIND),
    VariableMapping("V10M", CanonicalVar.NORTHWARD_WIND),
    VariableMapping("SWGDN", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("LWGAB", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    VariableMapping("PRECTOTCORR", CanonicalVar.PRECIPITATION_FLUX),  # already kg m-2 s-1
]


def _stream(year: int) -> int:
    for y0, y1, s in _STREAM_MAP:
        if y0 <= year <= y1:
            return s
    return 400


def _opendap_url(collection: str, short: str, year: int, month: int, day: int) -> str:
    return (
        f"{OPENDAP_BASE}/{collection}/{year}/{month:02d}/"
        f"MERRA2_{_stream(year)}.{short}.{year}{month:02d}{day:02d}.nc4"
    )


@register("merra2")
class MERRA2Connector(EarthdataAuthMixin, BaseForcingConnector):
    slug = "merra2"
    display_name = "NASA MERRA-2 (0.5°×0.625°, hourly, via OPeNDAP)"
    base_url = OPENDAP_BASE
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:single_levels",
                provider=self.slug,
                name="MERRA-2 surface forcing (hourly)",
                description=(
                    "NASA MERRA-2 hourly surface reanalysis merged from the SLV, "
                    "RAD and FLX collections via Earthdata-authenticated OPeNDAP."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.5,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.OPENDAP,
                license="NASA public data (open).",
                citation="Gelaro et al. (2017), MERRA-2, J. Climate 30:5419-5454.",
            )
        ]

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = {m.source_name for m in _MAPPINGS
                  if variables is None or m.canonical in set(variables)}
        # Only open collections that hold a requested variable.
        active = {k: v for k, v in _COLLECTIONS.items() if set(v[2]) & wanted}
        if not active:
            raise SubsetError("None of the requested variables are offered by MERRA-2")

        day = time_range.start.date()
        end = time_range.end.date()
        days = []
        while day <= end:
            days.append(day)
            day = day + timedelta(days=1)

        def _piece(d):
            col_ds = []
            for collection, short, present in active.values():
                url = _opendap_url(collection, short, d.year, d.month, d.day)
                ds = self._open_opendap(url)
                keep = [v for v in present if v in ds.data_vars and v in wanted]
                ds = ds[keep]
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                col_ds.append(apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon"))
            return xr.merge(col_ds, join="inner") if len(col_ds) > 1 else col_ds[0]

        pieces = await self._gather_pieces([partial(_piece, d) for d in days])

        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        ds_all = ds_all.sel(time=slice(time_range.start, time_range.end))
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No MERRA-2 data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="MERRA-2 via GES DISC OPeNDAP; SLV+RAD+FLX merged; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
