# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""TerraClimate connector — global monthly climate & water balance via THREDDS OPeNDAP.

TerraClimate (Abatzoglou et al.) is a global ~4 km (1/24°) **monthly** dataset of
climate and climatic water balance, served anonymously from the University of
Idaho Climatology Lab THREDDS as one aggregated NetCDF per variable.

This is monthly climatological / water-balance forcing — NOT a sub-daily or daily
forcing source. A consumer driving an hourly/daily land-surface model cannot use
it directly without temporal disaggregation; it is appropriate for monthly water
balance, anomaly, or climatological-forcing work.

Each forcing-relevant variable lives in its own aggregation
(``agg_terraclimate_{var}_1950_CurrentYear_GLOBE.nc``); the connector opens only
the ones the request needs, subsets the bbox + month range, and harmonizes:

  * ``tmax`` / ``tmin`` (°C) → a mean ``air_temperature`` = (tmax + tmin) / 2 in K
    (TerraClimate ships no monthly mean temperature; the midpoint is the standard
    proxy),
  * ``ppt`` (mm accumulated per month) → ``precipitation_flux`` (kg m-2 s-1): a
    month's accumulation divided by *that month's* seconds — computed per timestep
    from the calendar (month length varies), not a constant scale,
  * ``srad`` (W m-2) → ``surface_downwelling_shortwave_flux`` (identity),
  * ``ws`` (m s-1, scalar) → ``wind_speed`` (identity; TerraClimate has no u/v).

Native arrays are Int16/Int32 with ``scale_factor``/``add_offset``; xarray applies
them on open (``mask_and_scale`` default), so values arrive already in physical
units. The grid is a regular 1/24° lat/lon (``lat`` descending, ``lon`` −180..180)
plus a singleton ``crs`` coordinate that is dropped.

Needs pydap (the ``earthdata`` extra) for anonymous OPeNDAP. Anonymous, so
live-verifiable.
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

THREDDS_ROOT = "http://thredds.northwestknowledge.net:8080/thredds/dodsC"

# Each canonical variable's TerraClimate source dependency. air_temperature is
# derived from tmax+tmin; precipitation_flux from the monthly ppt accumulation;
# the rest are identity. (swe/srad use a hyphen in some HTML listings, but the
# dodsC aggregations are all the underscore `_1950_CurrentYear` form.)
_SOURCE_VARS: dict[CanonicalVar, tuple[str, ...]] = {
    CanonicalVar.AIR_TEMPERATURE: ("tmax", "tmin"),
    CanonicalVar.PRECIPITATION_FLUX: ("ppt",),
    CanonicalVar.SHORTWAVE_RADIATION_DOWN: ("srad",),
    CanonicalVar.WIND_SPEED: ("ws",),
}

# Mappings apply to the *derived* names the connector assigns (tair, ppt_flux)
# and the identity-SI source names (srad, ws).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tair", CanonicalVar.AIR_TEMPERATURE, offset=273.15,
                    note="mean of TerraClimate tmax/tmin (degC) -> K"),
    VariableMapping("ppt_flux", CanonicalVar.PRECIPITATION_FLUX,
                    note="TerraClimate monthly ppt (mm) / seconds-in-month -> kg m-2 s-1"),
    VariableMapping("srad", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("ws", CanonicalVar.WIND_SPEED),
]


def _agg_url(var: str) -> str:
    return f"{THREDDS_ROOT}/agg_terraclimate_{var}_1950_CurrentYear_GLOBE.nc"


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


@register("terraclimate")
class TerraClimateConnector(BaseForcingConnector):
    slug = "terraclimate"
    display_name = "TerraClimate (global monthly, ~4 km)"
    base_url = THREDDS_ROOT
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:monthly",
                provider=self.slug,
                name="TerraClimate global monthly climate & water balance (~4 km)",
                description=(
                    "University of Idaho TerraClimate global monthly fields, opened "
                    "through the Climatology Lab THREDDS OPeNDAP. MONTHLY resolution "
                    "— climatological/water-balance forcing, not sub-daily."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=1.0 / 24.0,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180.0, min_lat=-90.0, max_lon=180.0, max_lat=90.0),
                temporal=TemporalExtent(resolution=TemporalResolution.MONTHLY),
                protocol=Protocol.OPENDAP,
                license="CC0-1.0 (public domain); cite Abatzoglou et al. 2018, Sci. Data 5:170191.",
                citation=(
                    "Abatzoglou, J.T., et al. (2018), TerraClimate, a high-resolution "
                    "global dataset of monthly climate and climatic water balance, "
                    "Sci. Data 5:170191."
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

        wanted = set(variables) if variables else set(_SOURCE_VARS)
        wanted &= set(_SOURCE_VARS)
        if not wanted:
            raise SubsetError("None of the requested variables are offered by TerraClimate")

        # The union of native aggregations the requested canonical vars depend on.
        source_vars: list[str] = []
        for canon in _SOURCE_VARS:
            if canon in wanted:
                source_vars += [s for s in _SOURCE_VARS[canon] if s not in source_vars]

        parts = []
        for var in source_vars:
            ds = _open_anonymous_opendap(_agg_url(var))
            ds = ds.drop_vars("crs", errors="ignore")
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            parts.append(ds[[var]].sel(time=slice(time_range.start, time_range.end)))

        merged = xr.merge(parts, join="inner") if len(parts) > 1 else parts[0]
        if merged.sizes.get("time", 0) == 0:
            raise SubsetError(f"No TerraClimate data in [{time_range.start}, {time_range.end}]")

        # Derive the mean-temperature and per-month precip-flux fields the mappings
        # expect. Month length varies, so the flux divisor is computed per timestep.
        if CanonicalVar.AIR_TEMPERATURE in wanted:
            merged = merged.assign(tair=(merged["tmax"] + merged["tmin"]) / 2.0)
        if CanonicalVar.PRECIPITATION_FLUX in wanted:
            seconds = merged["time"].dt.days_in_month.astype("float64") * 86400.0
            merged = merged.assign(ppt_flux=merged["ppt"] / seconds)

        canonical = harmonize(merged, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="TerraClimate via Climatology Lab THREDDS OPeNDAP; monthly; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=True,
        )
