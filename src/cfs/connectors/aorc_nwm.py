# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""AORC NWM connector — NOAA AORC v1.1 on the NWM 1km LCC grid (S3 Zarr).

This is the projected version of AORC v1.1, aligned with the National Water
Model (NWM) v3.0 retrospective domain. It uses a 1km Lambert Conformal Conic
(LCC) grid over CONUS.

The archive hosts variables in separate Zarr stores on the public
``noaa-nwm-retrospective-3-0-pds`` bucket. Latitude and longitude coordinates
are generated dynamically from the LCC projection parameters.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
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

NWM_BUCKET = "noaa-nwm-retrospective-3-0-pds"
FORCING_PREFIX = "CONUS/zarr/forcing"

# NWM v3.0 LCC Projection Parameters
_PROJ_LCC = (
    "+proj=lcc +lat_1=30 +lat_2=60 +lat_0=40 +lon_0=-97 "
    "+x_0=0 +y_0=0 +a=6370000 +b=6370000 +units=m +no_defs"
)

# Native AORC-NWM names → canonical. All are identity SI.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("T2D", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("Q2D", CanonicalVar.SPECIFIC_HUMIDITY),
    VariableMapping("PSFC", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping("U2D", CanonicalVar.EASTWARD_WIND),
    VariableMapping("V2D", CanonicalVar.NORTHWARD_WIND),
    VariableMapping("SWDOWN", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("LWDOWN", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    VariableMapping("RAINRATE", CanonicalVar.PRECIPITATION_FLUX),
]

# Map canonical var to Zarr store name
_STORES = {
    CanonicalVar.AIR_TEMPERATURE: "t2d.zarr",
    CanonicalVar.SPECIFIC_HUMIDITY: "q2d.zarr",
    CanonicalVar.SURFACE_AIR_PRESSURE: "psfc.zarr",
    CanonicalVar.EASTWARD_WIND: "u2d.zarr",
    CanonicalVar.NORTHWARD_WIND: "v2d.zarr",
    CanonicalVar.SHORTWAVE_RADIATION_DOWN: "swdown.zarr",
    CanonicalVar.LONGWAVE_RADIATION_DOWN: "lwdown.zarr",
    CanonicalVar.PRECIPITATION_FLUX: "precip.zarr",
}


@register("aorc_nwm")
class AORCNWMConnector(ZarrStoreMixin, BaseForcingConnector):
    slug = "aorc_nwm"
    display_name = "NOAA AORC v1.1 NWM-Projected (1 km, hourly)"
    base_url = f"s3://{NWM_BUCKET}/{FORCING_PREFIX}"
    protocol = "zarr"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._grid_cache: tuple[np.ndarray, np.ndarray] | None = None

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:conus_1km",
                provider=self.slug,
                name="AORC v1.1 NWM-Projected forcing (1 km, hourly)",
                description=(
                    "NOAA Analysis of Record for Calibration, projected to the "
                    "NWM v3.0 1km LCC grid. Retrieved from the NWM retrospective "
                    "Zarr archive on AWS S3."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.00833,  # ~1 km
                crs="EPSG:4326",  # Output is lat/lon coords on the grid
                bbox=BoundingBox(min_lon=-134.0, min_lat=21.0, max_lon=-60.0, max_lat=53.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.ZARR,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NWS Analysis of Record for Calibration (AORC) v1.1.",
            )
        ]

    def _generate_grid(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Generate 2-D lat/lon coordinates from x/y metres via pyproj."""
        from pyproj import Transformer
        
        # Grid index to metres: Zarr has x/y coords in metres already.
        xx, yy = np.meshgrid(x, y)
        transformer = Transformer.from_crs(_PROJ_LCC, "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(xx, yy)
        return lat, lon

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

        wanted = set(variables) if variables else set(_STORES.keys())
        selected_mappings = [m for m in _MAPPINGS if m.canonical in wanted]
        if not selected_mappings:
            raise SubsetError("None of the requested variables are offered by AORC-NWM")

        cubes = []
        for mapping in selected_mappings:
            store_name = _STORES[mapping.canonical]
            path = f"{FORCING_PREFIX}/{store_name}"
            ds = self._open_s3_zarr(f"{NWM_BUCKET}/{path}", anonymous=True, consolidated=True)
            
            # Generate or cache the grid for the first variable.
            if self._grid_cache is None:
                lat, lon = self._generate_grid(ds.x.values, ds.y.values)
                self._grid_cache = (lat, lon)
            
            lat, lon = self._grid_cache
            ds = ds.assign_coords(latitude=(("y", "x"), lat), longitude=(("y", "x"), lon))
            
            # Subset 2-D grid and time.
            ds = subset_2d_grid(ds, bbox, lat_name="latitude", lon_name="longitude")
            ds = ds.sel(time=slice(time_range.start, time_range.end))
            
            if ds.sizes.get("time", 0) == 0:
                continue
            cubes.append(ds)

        if not cubes:
            raise SubsetError(f"No AORC-NWM data in [{time_range.start}, {time_range.end}]")
            
        ds_all = xr.merge(cubes, join="inner")
        canonical = harmonize(ds_all, selected_mappings, requested=variables, 
                              lat_name="latitude", lon_name="longitude")
        
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"AORC v1.1 NWM-Projected S3 Zarr ({NWM_BUCKET}); 2-D LCC subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
            ydim="y",
            xdim="x",
        )
