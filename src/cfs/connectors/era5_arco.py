# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ERA5 connector — ECMWF ERA5 reanalysis via Google Cloud ARCO-ERA5 (Zarr).

Migrated from SYMFLUENCE's ``ERA5ARCOAcquirer``, but reduced to the CFS boundary:
open the public ARCO Zarr store, subset to a bbox + time range, harmonize to the
canonical schema, and return a lazy ``xarray.Dataset``. Everything SYMFLUENCE did
*after* this — monthly chunking, ``_safe_to_netcdf`` HPC-locking fallbacks, and
``era5_to_summa_schema`` — stays in SYMFLUENCE, because it is model- and
deployment-specific, not part of acquiring canonical forcing.
"""

from __future__ import annotations

import time

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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

ARCO_STORE = "gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"

# Native ARCO-ERA5 variable → canonical schema, with linear unit conversions.
#  * total_precipitation: hourly accumulation in metres of water. To a mass flux
#    in kg m-2 s-1: depth[m] * 1000[kg m-3] / 3600[s] = depth * (1000/3600).
#  * *_radiation_downwards: hourly accumulation in J m-2. To a flux in W m-2:
#    energy[J m-2] / 3600[s].
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("2m_temperature", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("2m_dewpoint_temperature", CanonicalVar.DEWPOINT_TEMPERATURE),
    VariableMapping("10m_u_component_of_wind", CanonicalVar.EASTWARD_WIND),
    VariableMapping("10m_v_component_of_wind", CanonicalVar.NORTHWARD_WIND),
    VariableMapping("surface_pressure", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping(
        "total_precipitation",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1000.0 / 3600.0,
        note="ERA5 hourly accumulation (m) -> mass flux (kg m-2 s-1)",
    ),
    VariableMapping(
        "surface_solar_radiation_downwards",
        CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        scale=1.0 / 3600.0,
        note="ERA5 hourly accumulation (J m-2) -> flux (W m-2)",
    ),
    VariableMapping(
        "surface_thermal_radiation_downwards",
        CanonicalVar.LONGWAVE_RADIATION_DOWN,
        scale=1.0 / 3600.0,
        note="ERA5 hourly accumulation (J m-2) -> flux (W m-2)",
    ),
]


@register("era5_arco")
class ERA5ARCOConnector(ZarrStoreMixin, BaseForcingConnector):
    slug = "era5_arco"
    display_name = "ECMWF ERA5 (ARCO / Google Cloud)"
    base_url = f"gs://{ARCO_STORE}"
    protocol = "zarr"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:single_levels",
                provider=self.slug,
                name="ERA5 single levels (hourly, 0.25°)",
                description=(
                    "ERA5 reanalysis surface forcing from the Analysis-Ready, "
                    "Cloud-Optimized (ARCO) Zarr store on Google Cloud."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.ZARR,
                license="Copernicus / ECMWF licence (free, attribution)",
                citation="Hersbach et al. (2020), ERA5; ARCO-ERA5, Google/ECMWF.",
            )
        ]

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[object, FetchResult]:
        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        # ── Open the public ARCO Zarr store (lazy, anonymous) ───────────
        ds = self._open_zarr(ARCO_STORE, gcs_anonymous=True, consolidated=True)
        # Materialize only the coordinate axes so slicing is exact.
        ds = ds.assign_coords(
            longitude=ds.longitude.load(),
            latitude=ds.latitude.load(),
            time=ds.time.load(),
        )

        # ── Temporal subset ─────────────────────────────────────────────
        ds_t = ds.sel(time=slice(time_range.start, time_range.end))
        if ds_t.sizes.get("time", 0) == 0:
            raise SubsetError(
                f"No ERA5 time steps in [{time_range.start}, {time_range.end}]"
            )

        # ── Spatial subset (antimeridian + descending-lat aware) ────────
        plan = plan_bbox_subset(ds_t, bbox, lat_name="latitude", lon_name="longitude")
        ds_sub = apply_bbox_subset(ds_t, plan, lat_name="latitude", lon_name="longitude")

        # ── Harmonize to the canonical schema (stays lazy) ──────────────
        canonical = harmonize(ds_sub, _MAPPINGS, requested=variables)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"ARCO-ERA5 Zarr ({ARCO_STORE}); bbox+time subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
