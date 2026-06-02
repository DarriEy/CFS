# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NA-CORDEX connector — North America CORDEX regional climate projections (S3).

Acquires 0.22° and 0.44° daily CORDEX regional climate projections for North
America from the NCAR-managed public S3 bucket (anonymous). Following the
``nex_gddp`` pattern:

  * **experiment** (scenario) is the product id axis: ``na_cordex:hist``,
    ``na_cordex:rcp45``, ``na_cordex:rcp85``;
  * **model**, **grid**, and **bias-correction** are connector ``config`` knobs.

The native Zarr stores are already CF/SI, so all mappings are identity.
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
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

BUCKET = "ncar-na-cordex"
# Variable names in NA-CORDEX Zarr stores. All identity (CF/SI).
# Note: NA-CORDEX ships `hurs` (relative humidity, %) but neither specific humidity
# nor surface pressure, so q cannot be derived — `hurs` is deliberately not offered
# (mapping RH→specific_humidity would be a units error).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tas", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("pr", CanonicalVar.PRECIPITATION_FLUX),
    VariableMapping("rsds", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("uas", CanonicalVar.EASTWARD_WIND),
    VariableMapping("vas", CanonicalVar.NORTHWARD_WIND),
]

@register("na_cordex")
class NACORDEXConnector(ZarrStoreMixin, BaseForcingConnector):
    slug = "na_cordex"
    display_name = "NA-CORDEX (0.22°/0.44° daily, North America, via S3)"
    base_url = f"s3://{BUCKET}"
    protocol = "zarr"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # Defaults to the 22i (0.22°) grid and raw (no bias correction).
        self.grid = self.config.get("grid", "NAM-22i")
        self.bias_correction = self.config.get("bias_correction", "raw")

    async def list_products(self) -> list[ForcingProduct]:
        # NA-CORDEX combines historical+scenario into single stores; the eval run
        # is separate. (Confirmed against the ncar-na-cordex bucket listing.)
        exps = ["eval", "hist-rcp45", "hist-rcp85"]
        products = []
        for exp in exps:
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{exp}",
                    provider=self.slug,
                    name=f"NA-CORDEX {exp} daily ({self.grid})",
                    description=(
                        f"NA-CORDEX {exp} projection on {self.grid} grid with "
                        f"{self.bias_correction} bias-correction."
                    ),
                    variables=[
                        ProductVariable(canonical=m.canonical, source_name=m.source_name)
                        for m in _MAPPINGS
                    ],
                    resolution_deg=0.22 if "22" in self.grid else 0.44,
                    crs="EPSG:4326",
                    bbox=BoundingBox(min_lon=-179.0, min_lat=14.0, max_lon=-52.0, max_lat=83.0),
                    temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                    protocol=Protocol.ZARR,
                    license="NCAR / CORDEX (open).",
                    citation="Mearns et al. (2017), NA-CORDEX, NCAR.",
                )
            )
        return products

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[object, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        exp = product_id.split(":", 1)[1]
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else {m.canonical for m in _MAPPINGS}
        selected = [m for m in _MAPPINGS if m.canonical in wanted]
        
        # NA-CORDEX Zarr path pattern: day/{var}.{exp}.day.{grid}.{bias_correction}.zarr
        cubes = []
        for m in selected:
            path = f"day/{m.source_name}.{exp}.day.{self.grid}.{self.bias_correction}.zarr"
            try:
                ds = self._open_s3_zarr(f"{BUCKET}/{path}", anonymous=True, consolidated=True)
                ds = ds.sel(time=slice(time_range.start, time_range.end))
                # Spatial subset
                ds = ds.sel(lat=slice(bbox.min_lat, bbox.max_lat), lon=slice(bbox.min_lon, bbox.max_lon))
                
                if ds.sizes.get("time", 0) > 0:
                    cubes.append(ds)
            except Exception as e:
                logger.warning("na_cordex variable skip", var=m.source_name, error=str(e))
                continue

        if not cubes:
            raise SubsetError(f"No NA-CORDEX data in [{time_range.start}, {time_range.end}]")
            
        ds_all = xr.merge(cubes, join="inner")
        canonical = harmonize(ds_all, selected, requested=variables, lat_name="lat", lon_name="lon")
        
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"NA-CORDEX {exp} {self.grid} {self.bias_correction} (S3); canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
