# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""WFDE5 connector — bias-corrected ERA5 forcing via the Copernicus CDS.

WFDE5 is distributed on the CDS as monthly half-degree global land/lake NetCDF
files. The CDS product does not provide the same server-side area subset used by
ERA5-Land; this connector downloads the small monthly files to cache, opens them
lazily, subsets locally, and harmonizes to CFS canonical variables.

WFDE5 exposes rainfall and snowfall flux separately. CFS returns total
``precipitation_flux``, so the connector sums ``Rainf`` + ``Snowf`` before
harmonization. By default, precipitation uses the CRU+GPCC reference dataset,
while non-precipitation variables use CRU, matching the WFDE5 file convention.
"""

from __future__ import annotations

import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.cds_api import CDSAPIMixin
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

CDS_DATASET = "derived-near-surface-meteorological-variables"
# The CDS form requires `product` (wfde5 vs w5e5) and a underscore version token
# (e.g. "2_1" = v2.1), confirmed against the live dataset form constraints.
_PRODUCT = "wfde5"
_DEFAULT_VERSION = "2_1"
_PRECIP = "Precip"


@dataclass(frozen=True)
class _WFDE5Var:
    request_name: str
    nc_name: str
    canonical: CanonicalVar
    reference_dataset: str = "cru"


_DIRECT_VARS: list[_WFDE5Var] = [
    _WFDE5Var("near_surface_air_temperature", "Tair", CanonicalVar.AIR_TEMPERATURE),
    _WFDE5Var("near_surface_specific_humidity", "Qair", CanonicalVar.SPECIFIC_HUMIDITY),
    _WFDE5Var("surface_air_pressure", "PSurf", CanonicalVar.SURFACE_AIR_PRESSURE),
    _WFDE5Var("near_surface_wind_speed", "Wind", CanonicalVar.WIND_SPEED),
    _WFDE5Var("surface_downwelling_shortwave_radiation", "SWdown", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    _WFDE5Var("surface_downwelling_longwave_radiation", "LWdown", CanonicalVar.LONGWAVE_RADIATION_DOWN),
]
_RAIN = _WFDE5Var("rainfall_flux", "Rainf", CanonicalVar.PRECIPITATION_FLUX, "cru_and_gpcc")
_SNOW = _WFDE5Var("snowfall_flux", "Snowf", CanonicalVar.PRECIPITATION_FLUX, "cru_and_gpcc")

_MAPPINGS: list[VariableMapping] = [
    *[VariableMapping(v.nc_name, v.canonical) for v in _DIRECT_VARS],
    VariableMapping(_PRECIP, CanonicalVar.PRECIPITATION_FLUX, note="Rainf + Snowf total water-equivalent flux"),
]


def _months(time_range: TimeRange) -> list[tuple[int, int]]:
    y, m = time_range.start.year, time_range.start.month
    end_y, end_m = time_range.end.year, time_range.end.month
    out: list[tuple[int, int]] = []
    while (y, m) <= (end_y, end_m):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _extract_ncs(zip_path: Path, dest: Path) -> list[Path]:
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".nc"):
                continue
            target = dest / Path(member).name
            if not (target.exists() and target.stat().st_size > 0):
                with zf.open(member) as src, open(target, "wb") as fh:
                    fh.write(src.read())
            out.append(target)
    return out


@register("wfde5")
class WFDE5Connector(CDSAPIMixin, BaseForcingConnector):
    slug = "wfde5"
    display_name = "WFDE5 bias-corrected ERA5 forcing (0.5°, hourly)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    @property
    def _version(self) -> str:
        return str(self.config.get("version", _DEFAULT_VERSION))

    async def list_products(self) -> list[ForcingProduct]:
        variables = [ProductVariable(canonical=v.canonical, source_name=v.nc_name) for v in _DIRECT_VARS]
        variables.append(ProductVariable(canonical=CanonicalVar.PRECIPITATION_FLUX, source_name="Rainf+Snowf"))
        return [
            ForcingProduct(
                id=f"{self.slug}:hourly",
                provider=self.slug,
                name="WFDE5 hourly forcing (0.5° global land/lake)",
                description=(
                    "WATCH Forcing Data methodology applied to ERA5 (WFDE5), "
                    "bias-corrected hourly near-surface meteorology via the CDS."
                ),
                variables=variables,
                resolution_deg=0.5,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="Copernicus / ECMWF licence plus CRU/GPCC source-data terms; accept on CDS.",
                citation="Cucchi et al. (2020), WFDE5, Earth System Science Data 12:2097-2120.",
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
        direct = [v for v in _DIRECT_VARS if wanted is None or v.canonical in wanted]
        want_precip = wanted is None or CanonicalVar.PRECIPITATION_FLUX in wanted
        if not direct and not want_precip:
            raise SubsetError("None of the requested variables are offered by WFDE5")

        cache = self._cds_cache_dir()
        extract_dir = cache / f"extracted_v{self._version}"
        extract_dir.mkdir(parents=True, exist_ok=True)

        def _open_var(v: _WFDE5Var, year: int, month: int):
            target = cache / f"{self.slug}_{v.nc_name}_{v.reference_dataset}_{year}{month:02d}_v{self._version}.zip"
            request = {
                "product": _PRODUCT,
                "version": self._version,
                "variable": v.request_name,
                "reference_dataset": v.reference_dataset,
                "year": str(year),
                "month": f"{month:02d}",
                "format": "zip",
            }
            return target, request

        pieces = []
        for year, month in _months(time_range):
            month_pieces = []
            for v in direct:
                target, request = _open_var(v, year, month)
                await self._cds_retrieve(CDS_DATASET, request, target)
                ncs = _extract_ncs(target, extract_dir)
                if not ncs:
                    raise SubsetError(f"WFDE5 archive for {v.nc_name} held no .nc file")
                month_pieces.append(xr.open_dataset(ncs[0])[[v.nc_name]])

            if want_precip:
                precip_parts = []
                for v in (_RAIN, _SNOW):
                    target, request = _open_var(v, year, month)
                    await self._cds_retrieve(CDS_DATASET, request, target)
                    ncs = _extract_ncs(target, extract_dir)
                    if not ncs:
                        raise SubsetError(f"WFDE5 archive for {v.nc_name} held no .nc file")
                    precip_parts.append(xr.open_dataset(ncs[0])[[v.nc_name]])
                pr = xr.merge(precip_parts, join="inner")
                month_pieces.append(xr.Dataset({_PRECIP: pr[_RAIN.nc_name] + pr[_SNOW.nc_name]}))

            ds = xr.merge(month_pieces, join="inner") if len(month_pieces) > 1 else month_pieces[0]
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            pieces.append(ds.sel(time=slice(time_range.start, time_range.end)))

        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No WFDE5 data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"WFDE5 v{self._version} via CDS ({CDS_DATASET}); monthly full-grid "
                "NetCDF cache, local bbox+time subset; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                "WFDE5 CDS files are full half-degree monthly grids; CFS caches them and subsets locally."
            ],
        )
