# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Livneh CONUS daily gridded meteorology via NOAA PSL THREDDS OPeNDAP.

Gauge-based 1/16 deg (~6 km) daily surface meteorology over CONUS and southern
Canada (Livneh et al. 2015), opened anonymously through the NOAA PSL THREDDS
OPeNDAP server, one file per variable per year (``{var}.{YYYY}.nc``).

Livneh is a **partial** forcing source: it ships only precipitation, daily
max/min air temperature, and a (reanalysis-derived) scalar wind speed — there is
no radiation, humidity, or surface pressure (the same limitation as Daymet and
nClimGrid-Daily). So this connector offers four canonical fields:

  * ``prec`` (mm/day) -> ``precipitation_flux`` (kg m-2 s-1)
  * mean of ``tmax``/``tmin`` (degC) -> ``air_temperature`` (K). Livneh provides
    only daily max and min, so the canonical daily ``air_temperature`` is their
    midpoint ``(tmax + tmin) / 2`` — computed here before harmonization.
  * ``wind`` (m/s) -> ``wind_speed`` (a scalar, like NEX-GDDP/gridMET; not u/v)

The grid is regular 1/16 deg lat/lon with longitudes on the 0-360 convention
(235-293 degE), normalized on subset by :func:`cfs.subset.bbox.plan_bbox_subset`
exactly as for nClimGrid-Daily. Anonymous, so this connector is live-verifiable.
Needs the ``earthdata`` extra (pydap) for anonymous OPeNDAP.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

PSL_ROOT = "https://psl.noaa.gov/thredds/dodsC/Datasets/livneh/metvars"

# Canonical field -> the native per-variable file(s) it needs. ``air_temperature``
# is derived from the daily tmax/tmin pair (Livneh ships no daily mean).
_CANON_TO_SOURCES: dict[CanonicalVar, tuple[str, ...]] = {
    CanonicalVar.PRECIPITATION_FLUX: ("prec",),
    CanonicalVar.AIR_TEMPERATURE: ("tmax", "tmin"),
    CanonicalVar.WIND_SPEED: ("wind",),
}

_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        "prec",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 86400.0,
        note="Livneh daily precipitation (mm/day) -> flux (kg m-2 s-1)",
    ),
    VariableMapping(
        "tair",
        CanonicalVar.AIR_TEMPERATURE,
        offset=273.15,
        note="Livneh daily mean (tmax+tmin)/2 (degC) -> K",
    ),
    VariableMapping("wind", CanonicalVar.WIND_SPEED, note="Livneh near-surface scalar wind (m/s)"),
]


def _file_url(var: str, year: int) -> str:
    return f"{PSL_ROOT}/{var}.{year}.nc"


def _years(time_range: TimeRange) -> list[int]:
    return list(range(time_range.start.year, time_range.end.year + 1))


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


@register("livneh")
class LivnehConnector(BaseForcingConnector):
    slug = "livneh"
    display_name = "Livneh CONUS daily gridded meteorology (1/16°)"
    base_url = PSL_ROOT
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="Livneh daily CONUS meteorology (1/16°)",
                description=(
                    "Livneh et al. (2015) gauge-based daily precipitation, max/min "
                    "air temperature, and scalar wind over CONUS and southern Canada, "
                    "opened through NOAA PSL THREDDS OPeNDAP. Partial forcing: no "
                    "radiation, humidity, or surface pressure. air_temperature is the "
                    "daily mean (tmax+tmin)/2."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.0625,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-124.59, min_lat=25.16, max_lon=-67.03, max_lat=52.84),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="NOAA PSL / U.S. public domain (Livneh et al. 2015)",
                citation=(
                    "Livneh, B. et al. (2015), A spatially comprehensive, "
                    "hydrometeorological data set for Mexico, the U.S., and southern "
                    "Canada 1950-2013, Sci. Data 2:150042."
                ),
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

        wanted = set(variables) if variables else set(_CANON_TO_SOURCES)
        want_canon = [c for c in _CANON_TO_SOURCES if c in wanted]
        if not want_canon:
            raise SubsetError("None of the requested variables are offered by Livneh")
        need_sources = sorted({s for c in want_canon for s in _CANON_TO_SOURCES[c]})
        want_tair = CanonicalVar.AIR_TEMPERATURE in want_canon

        pieces = []
        for year in _years(time_range):
            per_var = {}
            for var in need_sources:
                ds = _open_anonymous_opendap(_file_url(var, year))
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
                per_var[var] = ds[var].sel(time=slice(time_range.start, time_range.end))
            merged = xr.Dataset(per_var)
            if want_tair:
                # Livneh ships only daily max/min; canonical daily air temperature
                # is their midpoint. Drop tmax/tmin afterward (not canonical fields).
                merged = merged.assign(tair=(merged["tmax"] + merged["tmin"]) / 2.0)
                merged = merged.drop_vars(["tmax", "tmin"])
            pieces.append(merged)

        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No Livneh data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="Livneh CONUS daily via NOAA PSL THREDDS OPeNDAP; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
