# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CHIRPS connector — Climate Hazards Group quasi-global daily precipitation.

Migrated from SYMFLUENCE's CHIRPS handler. CHIRPS publishes one plain-HTTP NetCDF
per year (a ~1.1 GB global file, no server-side subsetting). The files are
chunked HDF5 (netCDF-4, ~20×112×400 over time×lat×lon) on a range-capable server,
so this connector opens each covering year **lazily over HTTP byte-range** and
reads only the chunks overlapping the bbox + time window — a basin-scale subset
transfers a few MB instead of the whole 1.1 GB file. It then harmonizes the
``precip`` field (mm/day) to the canonical ``precipitation_flux`` (kg m-2 s-1)
and concatenates. A precip-only product → one canonical variable.

The subset itself is materialized (``lazy=False``), but no whole-year download
happens — so the old "download the world to clip a basin" cost is gone.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.http_files import HTTPFilesMixin
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

CHG_BASE = "https://data.chc.ucsb.edu/products/CHIRPS-2.0"
DAILY_PATH = "global_daily/netcdf/p05"
DAILY_FILE = "chirps-v2.0.{year}.days_p05.nc"
SECONDS_PER_DAY = 86400.0

# CHIRPS daily precip is mm/day → mass flux: /86400 (1 mm ≡ 1 kg m-2).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        "precip",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / SECONDS_PER_DAY,
        note="CHIRPS daily total (mm/day) -> flux (kg m-2 s-1)",
    ),
]


@register("chirps")
class CHIRPSConnector(HTTPFilesMixin, BaseForcingConnector):
    slug = "chirps"
    display_name = "CHIRPS v2.0 daily precipitation (0.05°)"
    base_url = CHG_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily_p05",
                provider=self.slug,
                name="CHIRPS v2.0 daily precipitation (0.05°)",
                description=(
                    "Climate Hazards Group InfraRed Precipitation with Station "
                    "data, quasi-global (50°S–50°N) 0.05° daily totals."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.05,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180.0, min_lat=-50.0, max_lon=180.0, max_lat=50.0),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.REST,
                license="CHIRPS is provided without restriction (CHG/UCSB).",
                citation="Funk et al. (2015), CHIRPS, Scientific Data 2:150066.",
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
        from cfs.core.config import get_settings

        settings = get_settings()
        self._guard_area(bbox, settings)

        years = range(time_range.start.year, time_range.end.year + 1)

        def _piece(year):
            url = f"{CHG_BASE}/{DAILY_PATH}/{DAILY_FILE.format(year=year)}"
            ds = self._open_http_lazy(url)
            plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
            ds_sp = apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude")
            ds_sp = ds_sp.sel(time=slice(time_range.start, time_range.end))
            # Materialize only the (small) overlapping chunks pulled over byte-range.
            return ds_sp.load() if ds_sp.sizes.get("time", 0) > 0 else None

        pieces = await self._gather_pieces([partial(_piece, y) for y in years])

        if not pieces:
            raise SubsetError(
                f"No CHIRPS data in [{time_range.start}, {time_range.end}] for the bbox"
            )
        ds_all = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
        canonical = harmonize(ds_all, _MAPPINGS, requested=variables)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="CHIRPS v2.0 daily p05 HDF5; HTTP byte-range subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
