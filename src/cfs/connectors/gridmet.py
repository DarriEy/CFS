# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""gridMET connector — daily CONUS surface meteorology via THREDDS OPeNDAP.

gridMET publishes daily ~4 km CONUS meteorology as one aggregated THREDDS
OPeNDAP dataset per variable. The store exposes long ``day`` axes from 1979 to
the current archive year and regular 1-D ``lat``/``lon`` coordinates, so this
connector opens only the requested variable datasets, subsets by bbox + day, and
harmonizes to CFS canonical variables.

Temperature is derived as the mean of gridMET's daily max/min temperature fields
(``tmmx``/``tmmn``). The other exposed fields are direct mappings.
"""

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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

THREDDS_ROOT = "http://thredds.northwestknowledge.net/thredds/dodsC"
_TMEAN = "daily_mean_temperature"


@dataclass(frozen=True)
class _GridmetVar:
    key: str
    nc_name: str
    canonical: CanonicalVar
    scale: float = 1.0
    offset: float = 0.0
    note: str = ""


_DIRECT_VARS: list[_GridmetVar] = [
    _GridmetVar(
        "pr",
        "precipitation_amount",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 86400.0,
        note="gridMET daily precipitation amount (mm/day) -> flux (kg m-2 s-1)",
    ),
    _GridmetVar("sph", "daily_mean_specific_humidity", CanonicalVar.SPECIFIC_HUMIDITY),
    _GridmetVar("vs", "daily_mean_wind_speed", CanonicalVar.WIND_SPEED),
    _GridmetVar("srad", "daily_mean_shortwave_radiation_at_surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
]

_MAPPINGS: list[VariableMapping] = [
    VariableMapping(_TMEAN, CanonicalVar.AIR_TEMPERATURE, note="mean(tmmx, tmmn)"),
    *[
        VariableMapping(v.nc_name, v.canonical, scale=v.scale, offset=v.offset, note=v.note)
        for v in _DIRECT_VARS
    ],
]


def _agg_url(key: str) -> str:
    return f"{THREDDS_ROOT}/agg_met_{key}_1979_CurrentYear_CONUS.nc"


def _open_anonymous_opendap(url: str):
    try:
        import pydap  # noqa: F401
        import xarray as xr
    except ImportError as e:  # pragma: no cover - only without optional deps
        raise MissingExtraError(
            "Anonymous OPeNDAP access needs the 'earthdata' extra for pydap: "
            "pip install -e '.[earthdata]'"
        ) from e
    return xr.open_dataset(url, engine="pydap")


def _subset_regular(ds, bbox: BoundingBox, time_range: TimeRange, *, time_name: str = "day"):
    plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
    ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
    return ds.sel({time_name: slice(time_range.start, time_range.end)})


@register("gridmet")
class GridMETConnector(BaseForcingConnector):
    slug = "gridmet"
    display_name = "gridMET daily CONUS meteorology (~4 km)"
    base_url = THREDDS_ROOT
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        variables = [ProductVariable(canonical=CanonicalVar.AIR_TEMPERATURE, source_name="mean(tmmx,tmmn)")]
        variables.extend(ProductVariable(canonical=v.canonical, source_name=v.nc_name) for v in _DIRECT_VARS)
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="gridMET daily surface meteorology (~4 km, CONUS)",
                description=(
                    "University of Idaho gridMET daily gridded surface meteorology "
                    "for CONUS, opened from Northwest Knowledge Network THREDDS OPeNDAP."
                ),
                variables=variables,
                resolution_deg=0.0416667,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-125.0, min_lat=25.0, max_lon=-66.0, max_lat=53.0),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="gridMET / University of Idaho Climatology Lab; see provider terms.",
                citation="Abatzoglou (2013), gridMET, Int. J. Climatol. 33:121-131.",
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
        want_tmean = wanted is None or CanonicalVar.AIR_TEMPERATURE in wanted
        direct = [v for v in _DIRECT_VARS if wanted is None or v.canonical in wanted]
        if not want_tmean and not direct:
            raise SubsetError("None of the requested variables are offered by gridMET")

        pieces = []
        if want_tmean:
            tmax = _subset_regular(_open_anonymous_opendap(_agg_url("tmmx")), bbox, time_range)[
                "daily_maximum_temperature"
            ]
            tmin = _subset_regular(_open_anonymous_opendap(_agg_url("tmmn")), bbox, time_range)[
                "daily_minimum_temperature"
            ]
            pieces.append(xr.Dataset({_TMEAN: (tmax + tmin) / 2.0}))

        for v in direct:
            ds = _subset_regular(_open_anonymous_opendap(_agg_url(v.key)), bbox, time_range)
            pieces.append(ds[[v.nc_name]])

        ds_all = xr.merge(pieces, join="inner") if len(pieces) > 1 else pieces[0]
        if ds_all.sizes.get("day", 0) == 0:
            raise SubsetError(f"No gridMET data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon", time_name="day")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="gridMET THREDDS OPeNDAP aggregates; bbox+day subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
