# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""HRRR connector — NOAA High-Resolution Rapid Refresh analysis (hrrrzarr S3).

The hardest store CFS wraps. The public ``hrrrzarr`` archive splits each
analysis hour into one Zarr group **per variable per level**
(``sfc/{YYYYMMDD}/{YYYYMMDD}_{HH}z_anl.zarr/{level}/{var}/{level}``), and those
groups carry **no latitude/longitude** — only ``projection_y/x_coordinate``
index dims on the HRRR Lambert Conformal Conic grid. So this connector:

  1. loads the static 2-D ``latitude``/``longitude`` grid once from the archive's
     ``grid/HRRR_chunk_index.zarr`` (no projection math needed — the lat/lon are
     published there directly), and computes the bbox index window
     (:func:`cfs.subset.grid2d.bbox_index_window`);
  2. for each hour, opens the needed variable groups, windows each to that
     index block, assembles them into one cube, attaches the windowed lat/lon,
     and concatenates over time;
  3. harmonizes — all fields are instantaneous SI, so mappings are identity.

HRRR **analysis** carries no precipitation, so ``precipitation_flux`` is not
offered. Per-variable-per-hour opens make long ranges slow (warned). Anonymous,
so this connector is live-verifiable.
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
from cfs.subset.grid2d import bbox_index_window

logger = structlog.get_logger()

GRID_KEY = "hrrrzarr/grid/HRRR_chunk_index.zarr"
SFC_PREFIX = "hrrrzarr/sfc"
_PROJ_Y, _PROJ_X = "projection_y_coordinate", "projection_x_coordinate"

# (GRIB level, GRIB short name, canonical). All instantaneous SI → identity.
_FIELDS: list[tuple[str, str, CanonicalVar]] = [
    ("2m_above_ground", "TMP", CanonicalVar.AIR_TEMPERATURE),
    ("2m_above_ground", "DPT", CanonicalVar.DEWPOINT_TEMPERATURE),
    ("2m_above_ground", "SPFH", CanonicalVar.SPECIFIC_HUMIDITY),
    ("surface", "PRES", CanonicalVar.SURFACE_AIR_PRESSURE),
    ("10m_above_ground", "UGRD", CanonicalVar.EASTWARD_WIND),
    ("10m_above_ground", "VGRD", CanonicalVar.NORTHWARD_WIND),
    ("surface", "DSWRF", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    ("surface", "DLWRF", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    ("surface", "PRATE", CanonicalVar.PRECIPITATION_FLUX),
]
_MAPPINGS: list[VariableMapping] = [VariableMapping(var, canon) for _, var, canon in _FIELDS]


@register("hrrr")
class HRRRConnector(ZarrStoreMixin, BaseForcingConnector):
    slug = "hrrr"
    display_name = "NOAA HRRR (3 km, hourly)"
    base_url = f"s3://{SFC_PREFIX}"
    protocol = "zarr"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._grid_cache = None

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:sfc_anl",
                provider=self.slug,
                name="HRRR surface analysis (3 km, hourly)",
                description=(
                    "NOAA High-Resolution Rapid Refresh 0-hour analysis from the "
                    "public hrrrzarr archive; LCC grid, lat/lon from the archive "
                    "grid file. No precipitation in the analysis stream."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS if m.canonical != CanonicalVar.PRECIPITATION_FLUX
                ],
                resolution_deg=0.027,  # ~3 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-134.1, min_lat=21.1, max_lon=-60.9, max_lat=52.6),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.ZARR,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP High-Resolution Rapid Refresh (HRRR); hrrrzarr archive.",
            ),
            ForcingProduct(
                id=f"{self.slug}:sfc_fcst",
                provider=self.slug,
                name="HRRR surface forecast (3 km, hourly)",
                description=(
                    "NOAA High-Resolution Rapid Refresh forecast (1-hour lead) from "
                    "the public hrrrzarr archive; includes precipitation flux."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.027,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-134.1, min_lat=21.1, max_lon=-60.9, max_lat=52.6),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.ZARR,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP High-Resolution Rapid Refresh (HRRR); hrrrzarr archive.",
            )
        ]

    def _grid(self):
        """Load and cache the static 2-D lat/lon grid (latitude, longitude over y, x)."""
        if self._grid_cache is None:
            g = self._open_s3_zarr(GRID_KEY, anonymous=True, consolidated=False)
            self._grid_cache = (g["latitude"].load(), g["longitude"].load())
        return self._grid_cache

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[object, FetchResult]:
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        is_fcst = product_id.endswith(":sfc_fcst")
        wanted = set(variables) if variables else None
        if not is_fcst and wanted is not None and CanonicalVar.PRECIPITATION_FLUX in wanted:
            raise SubsetError("HRRR analysis carries no precipitation_flux; use hrrr:sfc_fcst")
        
        selected = [f for f in _FIELDS if wanted is None or f[2] in wanted]
        # PRATE is only in fcst.
        if not is_fcst:
            selected = [f for f in selected if f[2] != CanonicalVar.PRECIPITATION_FLUX]

        if not selected:
            raise SubsetError("None of the requested variables are offered by HRRR")

        # Compute the index window once from the shared grid; window the lat/lon.
        lat2d, lon2d = self._grid()
        ys, xs = bbox_index_window(lat2d.values, lon2d.values, bbox, buffer=2)
        lat_w = lat2d.isel(y=ys, x=xs).values
        lon_w = lon2d.isel(y=ys, x=xs).values

        hours = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(ts):
            day = ts.strftime("%Y%m%d")
            suffix = "fcst" if is_fcst else "anl"
            zbase = f"{SFC_PREFIX}/{day}/{day}_{ts.hour:02d}z_{suffix}.zarr"
            data_vars = {}
            for level, var, _canon in selected:
                # In fcst, PRATE is usually at f01 (1-hour lead).
                path = f"{zbase}/{level}/{var}/{level}"
                try:
                    da = self._open_s3_zarr(path, anonymous=True, consolidated=False)[var]
                    # If it's a forecast, it might have a 'step' or 'time' dim.
                    # hrrrzarr fcst often has dims (step, y, x). We want step=1 (1-hour lead).
                    if "step" in da.dims:
                        da = da.sel(step=pd.Timedelta(hours=1))
                    
                    da = da.rename({_PROJ_Y: "y", _PROJ_X: "x"}).isel(y=ys, x=xs).astype("float32")
                    data_vars[var] = da
                except Exception as e:  # noqa: BLE001 - skip a missing/failed field
                    # list.append is atomic under the GIL — safe from worker threads.
                    warnings.append(f"{var}@{level} {ts:%Y-%m-%dT%H} ({suffix}) unavailable: {type(e).__name__}")
            if not data_vars:
                return None
            ds = xr.Dataset(data_vars).assign_coords(
                latitude=(("y", "x"), lat_w), longitude=(("y", "x"), lon_w)
            )
            # Forecast arrays may already carry a (length-1) time/step dim; collapse
            # any leftover non-spatial dims, then stamp the hour. Analysis arrays are
            # plain (y, x) and just get the time dim added.
            extra = [d for d in ds.dims if d not in ("y", "x")]
            if extra:
                ds = ds.isel({d: 0 for d in extra}, drop=True)
            ds = ds.expand_dims(time=[pd.Timestamp(ts)])
            return ds.load()

        pieces = await self._gather_pieces([lambda ts=ts: _piece(ts) for ts in hours])

        if not pieces:
            raise SubsetError(f"No HRRR data in [{time_range.start}, {time_range.end}] for the bbox")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables)
        if len(hours) > 6:
            warnings.append(
                f"HRRR opens {len(selected)} zarr groups per hour ({len(hours)} hours), "
                f"up to {settings.fetch_concurrency} hours concurrently — still slow for long ranges"
            )
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="HRRR hrrrzarr analysis; per-var groups assembled; grid-file lat/lon; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings,
            ydim="y",
            xdim="x",
        )
