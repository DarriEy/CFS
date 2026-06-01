# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Daymet connector — ORNL DAAC daily 1 km North America met, via Earthdata OPeNDAP.

Daymet V4R1 publishes one OPeNDAP granule per variable per year on the ORNL DAAC
(Earthdata-authenticated). The grid is Lambert Conformal Conic, indexed by 1-D
``x``/``y`` in metres (with 2-D ``lat``/``lon`` for reference). Because the full
North America grid is ~8 km × 8 km cells across the continent, subsetting by the
2-D lat/lon mask would mean loading enormous coordinate arrays — so instead the
bbox is projected to LCC ``x``/``y`` with ``pyproj`` and sliced directly (the
efficient route the OPeNDAP server supports).

Daymet is a *daily* product missing several forcing fields, so CFS derives only
what Daymet actually measures — no wind, surface pressure, longwave, or specific
humidity (the last would require surface pressure → elevation, which is an
*attribute*, outside the forcing boundary). Derived canonical variables:

  * ``air_temperature``      = (tmax + tmin)/2 + 273.15
  * ``precipitation_flux``   = prcp[mm/day] / 86400
  * ``surface_downwelling_shortwave_flux`` = srad[W/m² daylight] * dayl / 86400
  * ``dewpoint_temperature`` = inverse-Magnus(vp)   (see cfs.derive.humidity)

Auth-gated; the OPeNDAP structure/units were probe-confirmed with credentials.
"""

from __future__ import annotations

import time

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.earthdata import EarthdataAuthMixin
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
from cfs.derive.humidity import dewpoint_from_vapor_pressure
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

GRANULE = (
    "https://opendap.earthdata.nasa.gov/collections/C2532426483-ORNL_CLOUD/"
    "granules/Daymet_Daily_V4R1.daymet_v4_daily_na_{var}_{year}.nc"
)
DAYMET_PROJ4 = (
    "+proj=lcc +lat_1=25 +lat_2=60 +lat_0=42.5 +lon_0=-100 "
    "+x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs"
)
SECONDS_PER_DAY = 86400.0

# Internal derived-field name → canonical (physics already applied → identity).
_T = "_air_temperature"
_TD = "_dewpoint_temperature"
_PR = "_precipitation_flux"
_SW = "_shortwave"
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(_T, CanonicalVar.AIR_TEMPERATURE),
    VariableMapping(_TD, CanonicalVar.DEWPOINT_TEMPERATURE),
    VariableMapping(_PR, CanonicalVar.PRECIPITATION_FLUX),
    VariableMapping(_SW, CanonicalVar.SHORTWAVE_RADIATION_DOWN),
]
# canonical → the Daymet source variables it needs.
_NEEDS = {
    CanonicalVar.AIR_TEMPERATURE: ("tmax", "tmin"),
    CanonicalVar.DEWPOINT_TEMPERATURE: ("vp",),
    CanonicalVar.PRECIPITATION_FLUX: ("prcp",),
    CanonicalVar.SHORTWAVE_RADIATION_DOWN: ("srad", "dayl"),
}


@register("daymet")
class DaymetConnector(EarthdataAuthMixin, BaseForcingConnector):
    slug = "daymet"
    display_name = "Daymet V4R1 (1 km daily, North America, via OPeNDAP)"
    base_url = "https://opendap.earthdata.nasa.gov"
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily_v4",
                provider=self.slug,
                name="Daymet V4R1 daily (1 km, North America)",
                description=(
                    "ORNL DAAC Daymet daily 1 km surface weather over North "
                    "America via Earthdata OPeNDAP; LCC grid (x/y metres). "
                    "Temperature, precip, shortwave, and dewpoint (derived)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.009,  # ~1 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-179, min_lat=14, max_lon=-52, max_lat=83),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.OPENDAP,
                license="ORNL DAAC / NASA (open).",
                citation="Thornton et al. (2022), Daymet V4R1, ORNL DAAC.",
            )
        ]

    def _bbox_to_lcc(self, bbox: BoundingBox) -> tuple[float, float, float, float]:
        """Project the bbox corners to Daymet LCC and return (x_min,x_max,y_min,y_max)."""
        try:
            from pyproj import Transformer
        except ImportError as e:  # pragma: no cover
            raise MissingExtraError(
                "Daymet needs pyproj (the 'earthdata' extra): pip install -e '.[earthdata]'"
            ) from e
        tr = Transformer.from_crs("EPSG:4326", DAYMET_PROJ4, always_xy=True)
        lons = [bbox.min_lon, bbox.max_lon, bbox.min_lon, bbox.max_lon]
        lats = [bbox.min_lat, bbox.min_lat, bbox.max_lat, bbox.max_lat]
        xs, ys = tr.transform(lons, lats)
        buf = 1000.0  # 1 km buffer around the projected window
        return (min(xs) - buf, max(xs) + buf, min(ys) - buf, max(ys) + buf)

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

        wanted = set(variables) if variables else {m.canonical for m in _MAPPINGS}
        src_vars = sorted({v for c in wanted for v in _NEEDS.get(c, ())})
        if not src_vars:
            raise SubsetError("None of the requested variables are offered by Daymet")

        x_min, x_max, y_min, y_max = self._bbox_to_lcc(bbox)

        def _spatial(ds):
            # y is descending in Daymet (north→south); order the slice to match.
            yv = ds["y"].values
            y_slice = slice(y_max, y_min) if len(yv) > 1 and yv[0] > yv[-1] else slice(y_min, y_max)
            return ds.sel(x=slice(x_min, x_max), y=y_slice)

        years = range(time_range.start.year, time_range.end.year + 1)
        pieces = []
        for year in years:
            per_var = []
            for var in src_vars:
                # A DAP constraint requests only the fields we need — crucially
                # skipping Daymet's `time_bnds`, whose (time, 2) shape the xarray
                # pydap backend mis-parses and errors on.
                url = GRANULE.format(var=var, year=year) + f"?{var},time,x,y,lat,lon"
                ds = self._open_opendap(url)
                ds = _spatial(ds).sel(time=slice(time_range.start, time_range.end))
                per_var.append(ds)
            if per_var:
                merged = xr.merge(per_var, join="inner")
                if merged.sizes.get("time", 0) > 0:
                    pieces.append(merged.load())

        if not pieces:
            raise SubsetError(f"No Daymet data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        # Ensure 2-D lat/lon ride along as coordinates into the canonical cube.
        for c in ("lat", "lon"):
            if c in ds_all.data_vars:
                ds_all = ds_all.set_coords(c)

        # Derive the canonical fields from the Daymet inputs that are present.
        assigns = {}
        if {"tmax", "tmin"} <= set(ds_all.data_vars):
            assigns[_T] = (ds_all["tmax"] + ds_all["tmin"]) / 2.0 + 273.15
        if "vp" in ds_all.data_vars:
            assigns[_TD] = dewpoint_from_vapor_pressure(ds_all["vp"])
        if "prcp" in ds_all.data_vars:
            assigns[_PR] = ds_all["prcp"] / SECONDS_PER_DAY
        if {"srad", "dayl"} <= set(ds_all.data_vars):
            assigns[_SW] = ds_all["srad"] * ds_all["dayl"] / SECONDS_PER_DAY
        ds_all = ds_all.assign(assigns)

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="Daymet V4R1 via ORNL OPeNDAP; LCC x/y subset; derived T/Td/precip/SW; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            ydim="y",
            xdim="x",
        )
