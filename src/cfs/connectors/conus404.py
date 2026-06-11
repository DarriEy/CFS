# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CONUS404 connector — 4 km WRF reanalysis (HyTEST OSN Zarr).

USGS/NCAR CONUS404 hourly forcing, one analysis-ready Zarr datacube on the Open
Storage Network (anonymous, S3-compatible endpoint). The grid is Lambert
Conformal Conic with 2-D ``lat``/``lon`` over native ``y``/``x`` dims, so bbox
subsetting uses :mod:`cfs.subset.grid2d`.

Variable names/units were confirmed by a read-only probe. Most fields are
instantaneous SI (identity), with two accumulation cases handled here:

  * ``ACSWDNB``/``ACLWDNB`` — downwelling radiation accumulated in J m⁻² since
    model start → **de-accumulated** then ``/3600`` to W m⁻². A 1-hour pad is
    prepended before the window so the first returned step gets a real increment
    (its raw accumulated value would otherwise be enormous).
  * ``PREC_ACC_NC`` — precipitation accumulated over the 1-hour output bucket
    (mm, resets each step) → a **linear** ``/3600`` to kg m⁻² s⁻¹ (no
    de-accumulation: differencing independent buckets would be wrong).

Anonymous, so this connector is live-verifiable.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.zarr_store import ZarrStoreMixin
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
from cfs.subset.canonical import VariableMapping, harmonize
from cfs.subset.grid2d import subset_2d_grid

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

OSN_ENDPOINT = "https://usgs.osn.mghpcc.org/"
ZARR_KEY = "hytest/conus404/conus404_hourly.zarr"

# CONUS404 (WRF) native names → canonical.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("T2", CanonicalVar.AIR_TEMPERATURE),         # K
    VariableMapping("TD2", CanonicalVar.DEWPOINT_TEMPERATURE),   # K
    VariableMapping("Q2", CanonicalVar.SPECIFIC_HUMIDITY),       # kg/kg
    VariableMapping("PSFC", CanonicalVar.SURFACE_AIR_PRESSURE),  # Pa
    VariableMapping("U10", CanonicalVar.EASTWARD_WIND),          # m/s
    VariableMapping("V10", CanonicalVar.NORTHWARD_WIND),         # m/s
    VariableMapping(
        "ACSWDNB", CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        scale=1.0 / 3600.0, deaccumulate=True,
        note="WRF accumulated downwelling SW (J m-2) -> flux (W m-2)",
    ),
    VariableMapping(
        "ACLWDNB", CanonicalVar.LONGWAVE_RADIATION_DOWN,
        scale=1.0 / 3600.0, deaccumulate=True,
        note="WRF accumulated downwelling LW (J m-2) -> flux (W m-2)",
    ),
    VariableMapping(
        "PREC_ACC_NC", CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 3600.0,
        note="WRF 1-h bucket precip (mm) -> flux (kg m-2 s-1)",
    ),
]

# Variables whose first step needs a pre-window step to de-accumulate correctly.
_NEEDS_PAD = any(m.deaccumulate for m in _MAPPINGS)


@register("conus404")
class CONUS404Connector(ZarrStoreMixin, BaseForcingConnector):
    slug = "conus404"
    display_name = "CONUS404 (4 km WRF reanalysis, hourly)"
    base_url = f"s3://{ZARR_KEY}"
    protocol = "zarr"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:hourly",
                provider=self.slug,
                name="CONUS404 hourly forcing (4 km, CONUS)",
                description=(
                    "USGS/NCAR CONUS404 4 km WRF reanalysis, hourly, from the "
                    "HyTEST analysis-ready Zarr on Open Storage Network."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.036,  # ~4 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-134, min_lat=21, max_lon=-60, max_lat=53),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.ZARR,
                license="USGS/NCAR (public).",
                citation="Rasmussen et al. (2023), CONUS404; USGS HyTEST.",
            )
        ]

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        ds = self._open_s3_zarr(ZARR_KEY, anonymous=True, consolidated=True, endpoint_url=OSN_ENDPOINT)
        ds = subset_2d_grid(ds, bbox, lat_name="lat", lon_name="lon")  # isel y/x

        # Prepend a 1-hour pad so de-accumulated fields get a real first increment.
        pad_start = time_range.start - timedelta(hours=1) if _NEEDS_PAD else time_range.start
        ds = ds.sel(time=slice(pad_start, time_range.end))
        if ds.sizes.get("time", 0) == 0:
            raise SubsetError(f"No CONUS404 data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        # Drop the pad step now that de-accumulation has used it.
        canonical = canonical.sel(time=slice(time_range.start, time_range.end))
        if canonical.sizes.get("time", 0) == 0:
            raise SubsetError(f"No CONUS404 data in [{time_range.start}, {time_range.end}]")

        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="CONUS404 HyTEST OSN Zarr; 2-D LCC subset; radiation de-accumulated; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            ydim="y",
            xdim="x",
        )
