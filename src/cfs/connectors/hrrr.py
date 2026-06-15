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

HRRR **analysis** (``hrrr:sfc_anl``) carries no precipitation, so
``precipitation_flux`` is not offered there. Per-variable-per-hour opens make
long ranges slow (warned).

The **forecast** product (``hrrr:sfc_fcst``) instead reads GRIB2 from the
``noaa-hrrr-bdp-pds`` S3 archive with the Herbie ``.idx`` byte-range pattern
(shared :mod:`cfs.connectors.protocols.grib_idx`), following the same
forecast model as ``gfs``: the most recent hourly cycle at or before the
window start supplies every valid hour (``lead = valid − cycle``), out to f18
(or f48 for the 00/06/12/18Z runs). Every exposed field is instantaneous SI
(identity) — ``PRATE`` is an instantaneous precipitation flux (no
de-accumulation), ``DSWRF``/``DLWRF`` are instantaneous (no de-averaging), and
``SPFH`` is shipped directly (no humidity derivation). The 2-D LCC lat/lon
comes from cfgrib on decode, windowed with
:func:`cfs.subset.grid2d.subset_2d_grid`. Needs the ``forecast`` extra
(cfgrib/eccodes).

Anonymous, so both products are live-verifiable.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING, Any

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import cycle_for, http_range, parse_idx, read_field_2d
from cfs.connectors.protocols.zarr_store import ZarrStoreMixin
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
from cfs.subset.grid2d import bbox_index_window

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

GRID_KEY = "hrrrzarr/grid/HRRR_chunk_index.zarr"
SFC_PREFIX = "hrrrzarr/sfc"
_PROJ_Y, _PROJ_X = "projection_y_coordinate", "projection_x_coordinate"

# Analysis fields (hrrrzarr zarr groups), (zarr level, GRIB short name, canonical).
# All instantaneous SI → identity. The analysis stream carries no precipitation
# (PRATE is forecast-only, see _FCST_FIELDS).
_FIELDS: list[tuple[str, str, CanonicalVar]] = [
    ("2m_above_ground", "TMP", CanonicalVar.AIR_TEMPERATURE),
    ("2m_above_ground", "DPT", CanonicalVar.DEWPOINT_TEMPERATURE),
    ("2m_above_ground", "SPFH", CanonicalVar.SPECIFIC_HUMIDITY),
    ("surface", "PRES", CanonicalVar.SURFACE_AIR_PRESSURE),
    ("10m_above_ground", "UGRD", CanonicalVar.EASTWARD_WIND),
    ("10m_above_ground", "VGRD", CanonicalVar.NORTHWARD_WIND),
    ("surface", "DSWRF", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    ("surface", "DLWRF", CanonicalVar.LONGWAVE_RADIATION_DOWN),
]
_MAPPINGS: list[VariableMapping] = [VariableMapping(var, canon) for _, var, canon in _FIELDS]

# ── Forecast product (GRIB2 on noaa-hrrr-bdp-pds) ─────────────────────────────
# Surface forecast fields in the wrfsfc .idx, matched as (grib_var, grib_level).
# All instantaneous SI → identity: PRATE is an instantaneous flux (no
# de-accumulation), DSWRF/DLWRF are instantaneous (no de-averaging, unlike
# GFS/GEFS), SPFH is shipped directly (no derivation). Live-probed 2026-06-13.
GRIB_BASE = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
_FCST_FIELDS: list[tuple[str, str, CanonicalVar]] = [
    ("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE),
    ("SPFH", "2 m above ground", CanonicalVar.SPECIFIC_HUMIDITY),
    ("DPT", "2 m above ground", CanonicalVar.DEWPOINT_TEMPERATURE),
    ("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE),
    ("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND),
    ("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND),
    ("DSWRF", "surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    ("DLWRF", "surface", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    ("PRATE", "surface", CanonicalVar.PRECIPITATION_FLUX),
]
_FCST_MAPPINGS: list[VariableMapping] = [
    VariableMapping(var, canon) for var, _level, canon in _FCST_FIELDS
]
# Lead structure: f00–f18 every cycle; f00–f48 on the 00/06/12/18Z runs.
_MAX_LEAD_SHORT = 18
_MAX_LEAD_EXTENDED = 48
_EXTENDED_CYCLES = frozenset({0, 6, 12, 18})


def _fcst_max_lead(cycle_hour: int) -> int:
    return _MAX_LEAD_EXTENDED if cycle_hour in _EXTENDED_CYCLES else _MAX_LEAD_SHORT


def _fcst_url(cycle, lead: int) -> str:
    return (
        f"{GRIB_BASE}/hrrr.{cycle:%Y%m%d}/conus/"
        f"hrrr.t{cycle:%H}z.wrfsfcf{lead:02d}.grib2"
    )


@register("hrrr")
class HRRRConnector(ZarrStoreMixin, BaseForcingConnector):
    slug = "hrrr"
    display_name = "NOAA HRRR (3 km, hourly)"
    base_url = f"s3://{SFC_PREFIX}"
    protocol = "zarr"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._grid_cache: tuple[Any, Any] | None = None

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
                    "NOAA High-Resolution Rapid Refresh surface forecast, byte-range "
                    "read from the GRIB2 wrfsfc files on the noaa-hrrr-bdp-pds S3 "
                    "archive. The most recent hourly cycle at/before the requested "
                    "start supplies the valid-time forcing (leads to f18, or f48 for "
                    "the 00/06/12/18Z runs); includes instantaneous precipitation flux."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _FCST_MAPPINGS
                ],
                resolution_deg=0.027,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-134.1, min_lat=21.1, max_lon=-60.9, max_lat=52.6),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP High-Resolution Rapid Refresh (HRRR).",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "HRRR forecast GRIB2 decoding needs the 'forecast' extra: "
                "pip install -e '.[forecast]' (cfgrib + eccodes)"
            ) from e

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
    ) -> tuple[xr.Dataset, FetchResult]:
        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        if product_id.endswith(":sfc_fcst"):
            return await self._fetch_forecast(product, bbox, time_range, variables, settings, t0)
        return await self._fetch_analysis(product, bbox, time_range, variables, settings, t0)

    async def _fetch_analysis(
        self,
        product: ForcingProduct,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None,
        settings,
        t0: float,
    ) -> tuple[xr.Dataset, FetchResult]:
        """hrrrzarr 0-hour analysis: per-variable zarr groups assembled per hour."""
        import pandas as pd
        import xarray as xr

        wanted = set(variables) if variables else None
        if wanted is not None and CanonicalVar.PRECIPITATION_FLUX in wanted:
            raise SubsetError("HRRR analysis carries no precipitation_flux; use hrrr:sfc_fcst")
        # The analysis stream has no precipitation; offer everything else.
        selected = [
            f for f in _FIELDS
            if f[2] != CanonicalVar.PRECIPITATION_FLUX and (wanted is None or f[2] in wanted)
        ]
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
            zbase = f"{SFC_PREFIX}/{day}/{day}_{ts.hour:02d}z_anl.zarr"
            data_vars = {}
            for level, var, _canon in selected:
                path = f"{zbase}/{level}/{var}/{level}"
                try:
                    da = self._open_s3_zarr(path, anonymous=True, consolidated=False)[var]
                    da = da.rename({_PROJ_Y: "y", _PROJ_X: "x"}).isel(y=ys, x=xs).astype("float32")
                    data_vars[var] = da
                except Exception as e:  # noqa: BLE001 - skip a missing/failed field
                    # list.append is atomic under the GIL — safe from worker threads.
                    warnings.append(f"{var}@{level} {ts:%Y-%m-%dT%H} (anl) unavailable: {type(e).__name__}")
            if not data_vars:
                return None
            ds = xr.Dataset(data_vars).assign_coords(
                latitude=(("y", "x"), lat_w), longitude=(("y", "x"), lon_w)
            )
            ds = ds.expand_dims(time=[pd.Timestamp(ts)])
            return ds.load()

        pieces = await self._gather_pieces([partial(_piece, ts) for ts in hours])

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
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )

    async def _fetch_forecast(
        self,
        product: ForcingProduct,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None,
        settings,
        t0: float,
    ) -> tuple[xr.Dataset, FetchResult]:
        """GRIB2 surface forecast from noaa-hrrr-bdp-pds (Herbie .idx byte-range)."""
        import pandas as pd
        import xarray as xr

        self._require_cfgrib()
        wanted = set(variables) if variables else None
        selected = [f for f in _FCST_FIELDS if wanted is None or f[2] in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by HRRR forecast")

        cycle = cycle_for(time_range.start, step_h=1)
        max_lead = _fcst_max_lead(cycle.hour)
        valid_times = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not (0 <= lead <= max_lead):
                warnings.append(
                    f"HRRR lead f{lead:02d} (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz} (max f{max_lead:02d})"
                )
                return None
            url = _fcst_url(cycle, lead)
            idx = parse_idx(http_range(url + ".idx", 0, "").decode())
            # The grib_var is reused as the internal name; absent messages → None.
            per_var = [
                f for var, level, _c in selected
                if (f := read_field_2d(url, idx, var, level, var, bbox, label="HRRR")) is not None
            ]
            if not per_var:
                return None
            # per_var are disjoint single-variable cubes sharing the same grid coords.
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No HRRR forecast data in [{time_range.start}, {time_range.end}] from cycle "
                f"{cycle:%Y%m%d %Hz} (archive retains a long history; very old cycles age off)"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _FCST_MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"HRRR wrfsfc via noaa-hrrr-bdp-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )
