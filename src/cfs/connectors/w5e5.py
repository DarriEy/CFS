# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""W5E5 v2.0 connector — bias-corrected global daily forcing (ISIMIP repository).

WFDE5 over land merged with ERA5 over the ocean (W5E5 v2.0): the standard
bias-adjusted observational forcing behind ISIMIP3a, global 0.5°, daily,
1979–2019. Released **CC0-1.0** (public domain) — no attribution obligation,
though ISIMIP socially requests the dataset DOI citation.

Access is anonymous over plain HTTPS from the ISIMIP file server. Each climate
variable is its own ISIMIP dataset, stored as a handful of multi-year NetCDF4
chunks under a stable path::

    https://files.isimip.org/ISIMIP3a/SecondaryInputData/climate/atmosphere/
        obsclim/global/daily/historical/W5E5v2.0/{var}_W5E5v2.0_{start}-{end}.nc

The chunk boundaries are 1979–1980, then per-decade (1981–1990, … , 2011–2019).
Whole files are 0.5–2.4 GB, far too large to download for a basin subset — but
the server honours HTTP byte-range requests and the files are NetCDF4/HDF5, so
this connector opens them lazily through ``fsspec`` + ``h5netcdf`` and reads only
the chunks the bbox/time window touches (the native layout is one global slice
per day, ~1 MB).

Every W5E5 field is already canonical SI, so all mappings are identity — with one
shape caveat: **wind is the scalar ``sfcWind`` (m s⁻¹), not eastward/northward
components**, so it maps to canonical ``wind_speed`` (like NEX-GDDP / gridMET).
Requests for ``eastward_wind``/``northward_wind`` are not satisfiable from W5E5.

Needs the ``climate`` extra (fsspec + aiohttp + h5netcdf). Anonymous, so
live-verifiable.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, cast

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

ROOT = (
    "https://files.isimip.org/ISIMIP3a/SecondaryInputData/climate/atmosphere/"
    "obsclim/global/daily/historical/W5E5v2.0"
)

# ISIMIP/CF source name → canonical. Every field is canonical SI (identity).
# pr is already kg m-2 s-1; sfcWind is a SCALAR speed → wind_speed (W5E5 ships no
# u/v components). hurs/tasmax/tasmin/prsn/psl exist upstream but have no
# canonical-v1 counterpart and are intentionally omitted.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tas", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("pr", CanonicalVar.PRECIPITATION_FLUX),
    VariableMapping("huss", CanonicalVar.SPECIFIC_HUMIDITY),
    VariableMapping("ps", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping("rsds", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("rlds", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    VariableMapping("sfcWind", CanonicalVar.WIND_SPEED),
]

_FNAME_RE = re.compile(r"([A-Za-z]+)_W5E5v2\.0_(\d{8})-(\d{8})\.nc")


def _parse_chunk(fname: str) -> tuple[str, datetime, datetime] | None:
    """Parse ``{var}_W5E5v2.0_{YYYYMMDD}-{YYYYMMDD}.nc`` → (var, start, end)."""
    m = _FNAME_RE.fullmatch(fname)
    if not m:
        return None
    var, d0, d1 = m.group(1), m.group(2), m.group(3)
    return var, datetime.strptime(d0, "%Y%m%d"), datetime.strptime(d1, "%Y%m%d")


def _select_chunks(
    var: str, files: list[str], start: datetime, end: datetime
) -> list[str]:
    """Filenames for ``var`` whose [chunk_start, chunk_end] overlaps [start, end]."""
    out: list[tuple[datetime, str]] = []
    for f in files:
        parsed = _parse_chunk(f)
        if parsed is None or parsed[0] != var:
            continue
        _v, c0, c1 = parsed
        if c0 <= end and c1 >= start:
            out.append((c0, f))
    return [f for _c0, f in sorted(out)]


def _list_files() -> list[str]:
    """List the W5E5v2.0 directory (anonymous) → the NetCDF filenames present."""
    with urllib.request.urlopen(ROOT + "/", timeout=get_settings().provider_timeout_s) as r:  # noqa: S310 - https
        html = r.read().decode("utf-8", "ignore")
    return sorted(set(re.findall(r"[A-Za-z]+_W5E5v2\.0_\d{8}-\d{8}\.nc", html)))


def _open_remote(url: str):
    """Lazily open a remote NetCDF4 file via fsspec HTTP byte-range + h5netcdf."""
    try:
        import fsspec
        import xarray as xr
    except ImportError as e:  # pragma: no cover - only without the extra
        raise MissingExtraError(
            "W5E5 access needs the 'climate' extra (fsspec + aiohttp + h5netcdf): "
            "pip install -e '.[climate]'"
        ) from e
    return xr.open_dataset(fsspec.filesystem("https").open(url), engine="h5netcdf")


@register("w5e5")
class W5E5Connector(BaseForcingConnector):
    slug = "w5e5"
    display_name = "W5E5 v2.0 bias-corrected forcing (0.5°, daily, global)"
    base_url = ROOT
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:obsclim_daily",
                provider=self.slug,
                name="W5E5 v2.0 daily bias-corrected forcing (global 0.5°)",
                description=(
                    "WFDE5-over-land merged with ERA5-over-ocean (W5E5 v2.0), the "
                    "ISIMIP3a bias-adjusted observational forcing. Per-variable "
                    "NetCDF4 chunks on the ISIMIP file server, read lazily via HTTP "
                    "byte-range. Wind is the scalar sfcWind → wind_speed (no u/v)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.5,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(
                    start=datetime(1979, 1, 1),
                    end=datetime(2019, 12, 31),
                    resolution=TemporalResolution.DAILY,
                ),
                protocol=Protocol.REST,
                license="CC0-1.0 Public Domain (W5E5 v2.0; Lange et al., ISIMIP)",
                citation="Lange et al. (2021), W5E5 v2.0, ISIMIP. DOI 10.48364/ISIMIP.342217.",
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

        wanted = set(variables) if variables else None
        selected = [m for m in _MAPPINGS if wanted is None or m.canonical in wanted]
        if not selected:
            raise SubsetError(
                "None of the requested variables are offered by W5E5 "
                "(note: only scalar wind_speed, no eastward/northward wind)"
            )

        files = await asyncio.to_thread(_list_files)
        start, end = time_range.start, time_range.end

        def _read_var(src: str) -> xr.Dataset | None:
            parts = []
            for fname in _select_chunks(src, files, start, end):
                ds = _open_remote(f"{ROOT}/{fname}")
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
                ds = ds[[src]].sel(time=slice(start, end))
                if ds.sizes.get("time", 0) > 0:
                    parts.append(ds.load())
            if not parts:
                return None
            merged = xr.concat(parts, dim="time").sortby("time") if len(parts) > 1 else parts[0]
            return cast("xr.Dataset", merged)

        # Read each variable's chunks (blocking HDF5/byte-range opens) off the loop.
        results = await asyncio.to_thread(
            lambda: [_read_var(m.source_name) for m in selected]
        )
        per_var = [d for d in results if d is not None]
        if not per_var:
            raise SubsetError(
                f"No W5E5 data in [{start}, {end}] (coverage is 1979-01-01 to 2019-12-31)"
            )
        ds_all = xr.merge(per_var, join="inner")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="W5E5 v2.0 via ISIMIP file server (HTTP byte-range, h5netcdf); canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
