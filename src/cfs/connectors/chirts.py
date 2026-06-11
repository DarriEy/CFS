# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CHIRTS connector — Climate Hazards Group quasi-global daily temperature.

CHIRTSdaily is the temperature companion to CHIRPS. The Climate Hazards Center
publishes daily max/min 2 m air temperature on a global 0.05° grid (~60°S–70°N),
v1.0 covering 1983–2016. Each variable is one plain-HTTP NetCDF per year (a
~27 GB global file, no server-side subsetting). The files are chunked HDF5
(netCDF-4, ~1×520×1440 over time×lat×lon) on a range-capable server, so this
connector opens each covering year **lazily over HTTP byte-range** and reads
only the chunks overlapping the bbox + time window — a basin-scale subset
transfers a few MB instead of the whole file.

CHIRTS has no precipitation (that lives in CHIRPS). Temperature is derived as
the mean of the daily max/min fields and shifted from °C to Kelvin, mirroring
how gridMET/nClimGrid build a mean-temperature field, then harmonized identity
to the canonical ``air_temperature`` (K). The subset itself is materialized
(``lazy=False``), but no whole-year download happens.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.http_files import HTTPFilesMixin
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

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

CHC_BASE = "https://data.chc.ucsb.edu/products/CHIRTSdaily/v1.0/global_netcdf_p05"
TMAX_FILE = "Tmax/Tmax.{year}.nc"
TMIN_FILE = "Tmin/Tmin.{year}.nc"
CELSIUS_TO_KELVIN = 273.15

# CHIRTS years available in v1.0 (1983–2016, inclusive).
CHIRTS_YEAR_MIN = 1983
CHIRTS_YEAR_MAX = 2016

_TMEAN = "daily_mean_temperature"

# Internal mean field (°C) -> canonical air_temperature (K): identity + 273.15.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        _TMEAN,
        CanonicalVar.AIR_TEMPERATURE,
        offset=CELSIUS_TO_KELVIN,
        note="CHIRTS mean(Tmax, Tmin) (degC) -> air_temperature (K)",
    ),
]


def _year_url(template: str, year: int) -> str:
    return f"{CHC_BASE}/{template.format(year=year)}"


@register("chirts")
class CHIRTSConnector(HTTPFilesMixin, BaseForcingConnector):
    slug = "chirts"
    display_name = "CHIRTS v1.0 daily temperature (0.05°)"
    base_url = CHC_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="CHIRTS v1.0 daily temperature (0.05°)",
                description=(
                    "Climate Hazards Center InfraRed Temperature with Station data, "
                    "quasi-global (60°S–70°N) 0.05° daily 2 m air temperature. "
                    "Canonical air_temperature is the mean of the daily Tmax/Tmin "
                    "fields. v1.0 covers 1983–2016."
                ),
                variables=[
                    ProductVariable(canonical=CanonicalVar.AIR_TEMPERATURE, source_name="mean(Tmax,Tmin)"),
                ],
                resolution_deg=0.05,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180.0, min_lat=-60.0, max_lon=180.0, max_lat=70.0),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.REST,
                license="CHIRTS is provided without restriction (CHC/UCSB).",
                citation="Funk et al. (2019), CHIRTSdaily, Climate Hazards Center, UCSB.",
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
        from cfs.core.config import get_settings

        settings = get_settings()
        self._guard_area(bbox, settings)

        if variables and CanonicalVar.AIR_TEMPERATURE not in variables:
            raise SubsetError("CHIRTS only offers air_temperature")

        years = range(time_range.start.year, time_range.end.year + 1)

        def _piece(year):
            tmax = self._subset_year(TMAX_FILE, year, bbox, time_range)
            tmin = self._subset_year(TMIN_FILE, year, bbox, time_range)
            if tmax is None or tmin is None:
                return None
            tmean = (tmax["Tmax"] + tmin["Tmin"]) / 2.0
            return xr.Dataset({_TMEAN: tmean}).load()

        pieces = await self._gather_pieces([partial(_piece, y) for y in years])

        if not pieces:
            raise SubsetError(
                f"No CHIRTS data in [{time_range.start}, {time_range.end}] for the bbox"
            )
        ds_all = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
        canonical = harmonize(ds_all, _MAPPINGS, requested=variables)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="CHIRTS v1.0 daily p05 HDF5; HTTP byte-range subset; mean(Tmax,Tmin); canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )

    def _subset_year(self, template: str, year: int, bbox: BoundingBox, time_range: TimeRange):
        """Open one yearly file lazily and return the bbox+time subset (or None if empty)."""
        ds = self._open_http_lazy(_year_url(template, year))
        plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
        ds_sp = apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude")
        ds_sp = ds_sp.sel(time=slice(time_range.start, time_range.end))
        return ds_sp if ds_sp.sizes.get("time", 0) > 0 else None
