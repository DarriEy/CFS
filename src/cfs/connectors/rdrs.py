# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""RDRS / CaSR v3.2 connector — Canadian Surface Reanalysis via PAVICS OPeNDAP.

ECCC's CaSR v3.2 (successor to RDRS v2.1) at ~10 km hourly over North America,
served as one aggregated THREDDS/OPeNDAP NCML on PAVICS (anonymous). The grid is
a rotated pole with 2-D ``lat``/``lon`` over native ``rlat``/``rlon`` dims, so
bbox subsetting uses :mod:`cfs.subset.grid2d`.

Variable names and units were confirmed by a read-only probe of the store. CaSR
v3.2 is CF-compliant and already in SI — every mapping is identity, including
``pr`` (already kg m⁻² s⁻¹) and ``huss`` (specific humidity directly, so no RH
derivation is needed). Anonymous, so this connector is live-verifiable.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
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
from cfs.subset.canonical import VariableMapping, harmonize
from cfs.subset.grid2d import subset_2d_grid

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

OPENDAP_URL = (
    "https://pavics.ouranos.ca/twitcher/ows/proxy/thredds/dodsC/"
    "datasets/reanalyses/1hr_NAM_GovCan_CaSR_v32_198001-202412.ncml"
)

# CaSR v3.2 native (CF) names → canonical. All identity (already SI).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tas", CanonicalVar.AIR_TEMPERATURE),         # 1.5 m temperature, K
    VariableMapping("tdps", CanonicalVar.DEWPOINT_TEMPERATURE),   # 1.5 m dewpoint, K
    VariableMapping("huss", CanonicalVar.SPECIFIC_HUMIDITY),      # near-surface q, kg/kg
    VariableMapping("ps", CanonicalVar.SURFACE_AIR_PRESSURE),     # Pa
    VariableMapping("uas", CanonicalVar.EASTWARD_WIND),           # 10 m u, m/s
    VariableMapping("vas", CanonicalVar.NORTHWARD_WIND),          # 10 m v, m/s
    VariableMapping("rsds", CanonicalVar.SHORTWAVE_RADIATION_DOWN),  # W/m2
    VariableMapping("rlds", CanonicalVar.LONGWAVE_RADIATION_DOWN),   # W/m2
    VariableMapping("pr", CanonicalVar.PRECIPITATION_FLUX),       # already kg m-2 s-1
]


@register("rdrs")
class RDRSConnector(BaseForcingConnector):
    slug = "rdrs"
    display_name = "RDRS / CaSR v3.2 (Canadian Surface Reanalysis, ~10 km, hourly)"
    base_url = OPENDAP_URL
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:casr_v32",
                provider=self.slug,
                name="CaSR v3.2 hourly surface forcing (~10 km, NA)",
                description=(
                    "ECCC Canadian Surface Reanalysis v3.2 (RDRS successor), "
                    "rotated-pole grid, via the PAVICS THREDDS OPeNDAP aggregation."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.09,  # ~10 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=10, max_lon=180, max_lat=85),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.OPENDAP,
                license="Environment and Climate Change Canada (open data).",
                citation="ECCC, Canadian Surface Reanalysis (CaSR) v3.2 / RDRS.",
            )
        ]

    @staticmethod
    def _open(url: str):
        """Open the anonymous OPeNDAP aggregation lazily, WITHOUT dask.

        ``chunks={}`` (one whole-variable dask chunk) is a trap on this store:
        dask fused the rlat/rlon window into the DAP constraint but **not** the
        time slice, so every variable read pulled the full 45-year time axis
        for the spatial window (~128 MB/var; a 3-day fetch moved ~2.3 GB). A
        plain non-dask open uses xarray's lazy backend indexing, which pushes
        the time slice *and* the spatial window down into one small DAP
        hyperslab request per variable — the same access pattern as reading
        the store with netCDF4 directly.
        """
        import xarray as xr

        for engine in ("netcdf4", "pydap"):
            try:
                return xr.open_dataset(url, engine=engine, decode_times=True)
            except Exception as e:  # noqa: BLE001 - try the next engine
                last = e
        raise ConnectorError("rdrs", f"could not open OPeNDAP store: {last}")

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

        def _pull() -> xr.Dataset:
            ds = self._open(OPENDAP_URL)
            ds = subset_2d_grid(ds, bbox, lat_name="lat", lon_name="lon")  # isel rlat/rlon
            ds = ds.sel(time=slice(time_range.start, time_range.end))
            if ds.sizes.get("time", 0) == 0:
                raise SubsetError(f"No RDRS data in [{time_range.start}, {time_range.end}]")

            canonical: xr.Dataset = harmonize(
                ds, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon"
            )
            # Materialize exactly once, AFTER both subsets: one windowed DAP
            # hyperslab read per variable. The QC sample in _finalize and any
            # caller-side .load() then hit in-memory numpy instead of each
            # re-pulling from PAVICS (lazy handles don't cache reads).
            return canonical.load()

        canonical = await asyncio.to_thread(_pull)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="RDRS/CaSR v3.2 via PAVICS OPeNDAP; 2-D rotated-pole subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            ydim="rlat",
            xdim="rlon",
        )
