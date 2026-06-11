# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Shared base for the CDS regional reanalyses CARRA and CERRA.

These two differ from ERA5-Land in two structural ways that this base handles:

1. **Dual analysis + forecast streams.** Instantaneous fields (temperature, RH,
   wind, pressure) come from the ``analysis`` product type; accumulated fields
   (precip, radiation) only exist in the ``forecast`` stream at a 1-hour
   leadtime. We request both per month, shift the forecast valid-time back by the
   leadtime to align it onto the analysis time grid, and inner-merge. Because the
   forecast accumulation is over a single leadtime hour, those fields are a
   *linear* J m-2 / kg m-2 → flux conversion (``/3600``), not a daily-reset
   de-accumulation like ERA5-Land.

2. **Relative, not specific, humidity.** Both ship 2 m RH (%); specific humidity
   is derived via :mod:`cfs.derive.humidity` from RH, temperature and pressure.

Subclasses set the dataset name, grid, domain, longitude convention, the
analysis/forecast variable tables, and the NetCDF short names of the humidity
derivation inputs.

⚠ The CDS-specific strings here (NetCDF short names, the 3-hourly time list, the
``area``/longitude handling, CARRA's ``domain``) are best-effort from the
SYMFLUENCE handlers and the ecCodes conventions; they are auth-gated and so are
**not live-verified**. The first authenticated fetch should confirm them — the
code degrades gracefully (missing vars are skipped with a warning) rather than
crashing if a short name differs.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.cds_api import CDSAPIMixin
from cfs.core.config import get_settings
from cfs.core.exceptions import SubsetError
from cfs.core.models import BoundingBox, FetchResult, ForcingProduct, TimeRange
from cfs.core.vocabulary import CanonicalVar
from cfs.derive.humidity import specific_humidity_from_rh
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

# Standard 3-hourly synoptic steps for the merged CARRA/CERRA product.
DEFAULT_TIMES = ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
_DERIVED_Q = "specific_humidity_derived"


@dataclass(frozen=True)
class _RVar:
    """A directly-mapped reanalysis variable (CDS request name, NetCDF name)."""

    request_name: str
    nc_name: str
    canonical: CanonicalVar
    scale: float = 1.0
    note: str = ""


class CDSReanalysisConnector(CDSAPIMixin, BaseForcingConnector):
    """Base for CARRA/CERRA. Subclasses set the class attributes below."""

    # ── Subclass configuration ──────────────────────────────────────
    dataset: str = ""
    grid: list[float] = []  # CDS grid interpolation, e.g. [0.025, 0.025]
    domain: str | None = None  # CARRA west_domain/east_domain; None for CERRA
    extra_request: dict = {}  # e.g. {'level_type': 'surface_or_atmosphere', 'data_type': 'reanalysis'}
    times: list[str] = DEFAULT_TIMES
    leadtime: str = "1"
    resolution_deg: float = 0.025
    product_bbox: BoundingBox = BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90)
    license_str: str = "Copernicus / ECMWF licence (free, attribution; accept on CDS)."
    citation: str = ""
    analysis_vars: list[_RVar] = []
    forecast_vars: list[_RVar] = []
    # NetCDF short names of the humidity-derivation inputs (RH, T, pressure):
    rh_name: str = "r2"
    t_name: str = "t2m"
    p_name: str = "sp"

    # ── Request building (pure-ish; unit-tested) ────────────────────

    @staticmethod
    def _chunk_days(year: int, month: int, time_range: TimeRange) -> list[str]:
        """Days of ``year``/``month`` that fall within the request — so a short
        request doesn't trigger a whole-month CDS extraction (CARRA at 2.5 km is
        huge). All ``times`` are still requested per day, then trimmed by ``.sel``.
        """
        import datetime as _dt
        from calendar import monthrange

        ndays = monthrange(year, month)[1]
        first = max(_dt.date(year, month, 1), time_range.start.date())
        last = min(_dt.date(year, month, ndays), time_range.end.date())
        return [f"{d:02d}" for d in range(first.day, last.day + 1)]

    def _build_request(
        self,
        product_type: str,
        bbox: BoundingBox,
        year: int,
        month: int,
        var_request_names: list[str],
        days: list[str],
    ) -> dict:
        req: dict = {
            "product_type": product_type,
            "variable": var_request_names,
            "year": str(year),
            "month": f"{month:02d}",
            "day": days,
            "time": self.times,
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": self._cds_area(bbox),
            "grid": self.grid,
            **self.extra_request,
        }
        if self.domain:
            req["domain"] = self.domain
        if product_type == "forecast":
            req["leadtime_hour"] = self.leadtime
        return req

    @staticmethod
    def _align_forecast(ds_fc, leadtime_hours: int = 1):
        """Shift forecast valid-time back by the leadtime onto the analysis grid."""
        shifted = ds_fc["time"] - np.timedelta64(leadtime_hours, "h")
        return ds_fc.assign_coords(time=shifted)

    @staticmethod
    def _merge_streams(ds_an, ds_fc):
        """Inner-merge aligned analysis + forecast cubes on their shared times."""
        import xarray as xr

        if ds_an is None:
            return ds_fc
        if ds_fc is None:
            return ds_an
        return xr.merge([ds_an, ds_fc], join="inner")

    # ── Catalog ─────────────────────────────────────────────────────

    def _all_canonical(self) -> list[CanonicalVar]:
        out = [v.canonical for v in (*self.analysis_vars, *self.forecast_vars)]
        out.append(CanonicalVar.SPECIFIC_HUMIDITY)  # derived
        # preserve order, drop dups
        seen, uniq = set(), []
        for c in out:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    async def list_products(self) -> list[ForcingProduct]:
        from cfs.core.models import (
            ProductVariable,
            Protocol,
            TemporalExtent,
            TemporalResolution,
        )

        variables = [
            ProductVariable(canonical=v.canonical, source_name=v.nc_name)
            for v in (*self.analysis_vars, *self.forecast_vars)
        ]
        variables.append(
            ProductVariable(canonical=CanonicalVar.SPECIFIC_HUMIDITY, source_name=_DERIVED_Q)
        )
        return [
            ForcingProduct(
                id=f"{self.slug}:single_levels",
                provider=self.slug,
                name=self.display_name,
                description=(
                    f"{self.display_name} surface forcing via the Copernicus CDS "
                    "(analysis + forecast streams merged; RH→specific humidity derived)."
                ),
                variables=variables,
                resolution_deg=self.resolution_deg,
                crs="EPSG:4326",
                bbox=self.product_bbox,
                temporal=TemporalExtent(resolution=TemporalResolution.THREE_HOURLY),
                protocol=Protocol.REST,
                license=self.license_str,
                citation=self.citation,
            )
        ]

    # ── Fetch ───────────────────────────────────────────────────────

    def _cache_name(
        self, stream: str, bbox: BoundingBox, names: list[str], y: int, m: int, days: list[str]
    ) -> str:
        key = (
            f"{bbox.min_lon},{bbox.min_lat},{bbox.max_lon},{bbox.max_lat}"
            f"|{','.join(sorted(names))}|{days[0]}-{days[-1]}"
        )
        h = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:8]
        return f"{self.slug}_{stream}_{y}{m:02d}_{h}.nc"

    def _select(self, variables: list[CanonicalVar] | None):
        """Return (analysis _RVar list, forecast _RVar list, derive_q?) for the request."""
        wanted = set(variables) if variables else None
        want_q = wanted is None or CanonicalVar.SPECIFIC_HUMIDITY in wanted
        an = [v for v in self.analysis_vars if wanted is None or v.canonical in wanted]
        fc = [v for v in self.forecast_vars if wanted is None or v.canonical in wanted]
        return an, fc, want_q

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

        an_vars, fc_vars, want_q = self._select(variables)
        # Humidity derivation needs RH + T + P in the analysis request.
        an_request = [v.request_name for v in an_vars]
        if want_q:
            for extra in ("2m_relative_humidity",):
                if extra not in an_request:
                    an_request.append(extra)
            # Ensure T and P are fetched as derivation inputs even if not directly requested.
            for v in self.analysis_vars:
                if v.nc_name in (self.t_name, self.p_name) and v.request_name not in an_request:
                    an_request.append(v.request_name)
        fc_request = [v.request_name for v in fc_vars]
        if not an_request and not fc_request:
            raise SubsetError("None of the requested variables are offered by this product")

        cache = self._cds_cache_dir()
        merged_pieces = []
        for year, month in self._month_chunks(time_range):
            days = self._chunk_days(year, month, time_range)
            ds_an = ds_fc = None
            if an_request:
                target = cache / self._cache_name("an", bbox, an_request, year, month, days)
                await self._cds_retrieve(
                    self.dataset,
                    self._build_request("analysis", bbox, year, month, an_request, days), target
                )
                ds_an = self._cds_open(target)
            if fc_request:
                target = cache / self._cache_name("fc", bbox, fc_request, year, month, days)
                await self._cds_retrieve(
                    self.dataset,
                    self._build_request("forecast", bbox, year, month, fc_request, days), target
                )
                ds_fc = self._align_forecast(self._cds_open(target), int(self.leadtime))
            merged_pieces.append(self._merge_streams(ds_an, ds_fc))

        ds_all = (
            xr.concat(merged_pieces, dim="time").sortby("time")
            if len(merged_pieces) > 1
            else merged_pieces[0]
        )

        # Build the linear mappings, then derive specific humidity if possible.
        mappings = [
            VariableMapping(v.nc_name, v.canonical, scale=v.scale, note=v.note)
            for v in (*an_vars, *fc_vars)
        ]
        warnings: list[str] = []
        if want_q:
            if all(n in ds_all.data_vars for n in (self.rh_name, self.t_name, self.p_name)):
                q = specific_humidity_from_rh(
                    ds_all[self.rh_name], ds_all[self.t_name], ds_all[self.p_name]
                )
                ds_all = ds_all.assign({_DERIVED_Q: q})
                mappings.append(VariableMapping(_DERIVED_Q, CanonicalVar.SPECIFIC_HUMIDITY))
            else:
                warnings.append(
                    "specific_humidity not derived: missing one of RH/T/pressure "
                    f"({self.rh_name}/{self.t_name}/{self.p_name}) in the CDS output — "
                    "verify the NetCDF short names for this dataset"
                )

        canonical = harmonize(ds_all, mappings, requested=variables)
        canonical = canonical.sel(time=slice(time_range.start, time_range.end))
        if canonical.sizes.get("time", 0) == 0:
            raise SubsetError(
                f"No {self.slug} data in [{time_range.start}, {time_range.end}]"
            )

        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"{self.slug} via CDS ({self.dataset}); analysis+forecast merged; "
                "area subset; RH→q derived; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings,
        )
