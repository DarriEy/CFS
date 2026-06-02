# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NARR connector — NOAA/PSL daily North American Regional Reanalysis."""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.core.config import get_settings
from cfs.core.exceptions import MissingExtraError, SubsetError
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

logger = structlog.get_logger()

PSL_ROOT = "https://psl.noaa.gov/thredds/dodsC/Datasets/NARR/Dailies/monolevel"


@dataclass(frozen=True)
class _NarrVar:
    file_prefix: str
    nc_name: str
    canonical: CanonicalVar


_VARS: list[_NarrVar] = [
    _NarrVar("air.2m", "air", CanonicalVar.AIR_TEMPERATURE),
    _NarrVar("shum.2m", "shum", CanonicalVar.SPECIFIC_HUMIDITY),
    _NarrVar("pres.sfc", "pres", CanonicalVar.SURFACE_AIR_PRESSURE),
    _NarrVar("uwnd.10m", "uwnd", CanonicalVar.EASTWARD_WIND),
    _NarrVar("vwnd.10m", "vwnd", CanonicalVar.NORTHWARD_WIND),
    _NarrVar("prate", "prate", CanonicalVar.PRECIPITATION_FLUX),
    _NarrVar("dswrf", "dswrf", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    _NarrVar("dlwrf", "dlwrf", CanonicalVar.LONGWAVE_RADIATION_DOWN),
]

_MAPPINGS: list[VariableMapping] = [VariableMapping(v.nc_name, v.canonical) for v in _VARS]


def _yearly_url(file_prefix: str, year: int) -> str:
    return f"{PSL_ROOT}/{file_prefix}.{year}.nc"


def _years(time_range: TimeRange) -> range:
    return range(time_range.start.year, time_range.end.year + 1)


def _open_anonymous_opendap(url: str):
    try:
        import pydap  # noqa: F401
        import xarray as xr
    except ImportError as e:  # pragma: no cover
        raise MissingExtraError(
            "Anonymous OPeNDAP access needs the 'earthdata' extra for pydap: "
            "pip install -e '.[earthdata]'"
        ) from e
    return xr.open_dataset(url, engine="pydap")


@register("narr")
class NARRConnector(BaseForcingConnector):
    slug = "narr"
    display_name = "NOAA NARR daily monolevel fields (32 km)"
    base_url = PSL_ROOT
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="NARR daily monolevel surface forcing (32 km)",
                description=(
                    "NOAA/NCEP North American Regional Reanalysis daily monolevel "
                    "fields from NOAA PSL THREDDS. Grid is Lambert conformal with "
                    "2-D lat/lon over y/x."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.29,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-170.0, min_lat=1.0, max_lon=-20.0, max_lat=85.0),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="NOAA public data.",
                citation="Mesinger et al. (2006), North American Regional Reanalysis, BAMS 87:343-360.",
            )
        ]

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
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by NARR")

        cubes = []
        for v in selected:
            pieces = []
            for year in _years(time_range):
                ds = _open_anonymous_opendap(_yearly_url(v.file_prefix, year))
                ds = subset_2d_grid(ds[[v.nc_name, "lat", "lon"]], bbox, lat_name="lat", lon_name="lon")
                pieces.append(ds.sel(time=slice(time_range.start, time_range.end)))
            cubes.append(xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0])

        ds_all = xr.merge(cubes, join="inner") if len(cubes) > 1 else cubes[0]
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No NARR data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="NARR daily monolevel fields via NOAA PSL THREDDS OPeNDAP; 2-D LCC subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
            ydim="y",
            xdim="x",
        )
