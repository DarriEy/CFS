# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""GEFS connector — NOAA Global Ensemble Forecast System (AWS GRIB2).

The ensemble companion to :mod:`cfs.connectors.gfs`: the same ``.idx`` byte-range +
cfgrib machinery, but over the GEFS ensemble (control ``gec00`` + perturbations
``gep01``…``gep30``) on the public ``noaa-gefs-pds`` S3 archive. The returned cube
carries an extra ``member`` dimension.

Reads the 0.25° "select" product (``atmos/pgrb2sp25``), which carries the surface
fields at 3-hourly lead times (f000…f384). Forecast/cycle handling is identical to
GFS: the most recent 00/06/12/18 UTC cycle at/before the range start, valid times
mapped to (3-hourly) lead hours.

Variable scope (v1, deliberately conservative): the **instantaneous** surface
fields are exposed — ``TMP`` 2 m → air_temperature, ``PRES`` surface →
surface_air_pressure, ``UGRD``/``VGRD`` 10 m → eastward/northward_wind — all
identity SI. Precipitation (``APCP``) and radiation (``DSWRF``/``DLWRF``) are
**deferred**: GEFS accumulates/averages them in **6-hour buckets** (0-3, 0-6, 6-9,
6-12, …), so a correct per-interval flux needs reset-aware differencing within each
bucket — a focused follow-up rather than a risky conversion here. GEFS-select also
ships relative humidity (not specific humidity), so specific_humidity is not
offered either.

Members are selected via ``config={"members": [...]}`` (default: control + all 30
perturbations). The ensemble is the point, but a full 31-member pull is many
byte-range requests — restrict the member list for quick fetches. Needs the
``forecast`` extra (cfgrib/eccodes). Anonymous; live-verifiable.
"""

from __future__ import annotations

import os
import tempfile
import time
import urllib.request

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.gfs import _cycle_for, _parse_idx
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

S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"

# Default ensemble: control + 30 perturbations.
_ALL_MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]

# Instantaneous surface fields only (identity SI); .idx level strings matched exactly.
# (var, level, canonical, internal-name)
_VARS = [
    ("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE, "_t2m"),
    ("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp"),
    ("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND, "_u10"),
    ("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND, "_v10"),
]
_MAPPINGS: list[VariableMapping] = [VariableMapping(internal, canon) for _, _, canon, internal in _VARS]

_MAX_LEAD = 384  # 3-hourly


def _lead_available(lead: int) -> bool:
    return 0 <= lead <= _MAX_LEAD and lead % 3 == 0


def _file_url(member: str, cycle, lead: int) -> str:
    d = cycle.strftime("%Y%m%d")
    h = cycle.strftime("%H")
    return f"{S3_BASE}/gefs.{d}/{h}/atmos/pgrb2sp25/{member}.t{h}z.pgrb2s.0p25.f{lead:03d}"


def _http_range(url: str, start: int, end: int | str) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})  # noqa: S310 - https S3
    with urllib.request.urlopen(req, timeout=get_settings().provider_timeout_s) as r:
        return r.read()


def _open_message(raw: bytes, internal: str):
    """Decode one GRIB message (bytes) with cfgrib → single-variable Dataset."""
    import xarray as xr

    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""}).load()
    finally:
        os.unlink(path)
    data_vars = list(ds.data_vars)
    if not data_vars:
        raise SubsetError(f"GEFS message for {internal} decoded no variable")
    ds = ds[[data_vars[0]]].rename({data_vars[0]: internal})
    drop = [c for c in ds.coords if c not in ("latitude", "longitude")]
    return ds.drop_vars(drop, errors="ignore")


@register("gefs")
class GEFSConnector(BaseForcingConnector):
    slug = "gefs"
    display_name = "NOAA GEFS ensemble forecast (0.25° select, 3-hourly, global)"
    base_url = S3_BASE
    protocol = "http"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.members = list(self.config.get("members", _ALL_MEMBERS))

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:atmos_0p25",
                provider=self.slug,
                name="GEFS 0.25° select ensemble forecast (global)",
                description=(
                    "NOAA Global Ensemble Forecast System surface forcing (0.25° "
                    "select product), byte-range read from the GRIB2 files on the "
                    "noaa-gefs-pds S3 archive. Returns a member dimension (control + "
                    "perturbations). Instantaneous fields only in v1."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.THREE_HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP Global Ensemble Forecast System (GEFS).",
            )
        ]

    @staticmethod
    def _require_cfgrib():
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "GEFS GRIB2 decoding needs the 'forecast' extra: "
                "pip install -e '.[forecast]' (cfgrib + eccodes)"
            ) from e

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
        self._require_cfgrib()

        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v[2] in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by GEFS")
        if not self.members:
            raise SubsetError("GEFS member list is empty")

        cycle = _cycle_for(time_range.start)
        # GEFS-select is 3-hourly; snap the start down to a 3h boundary off the cycle.
        valid_times = pd.date_range(time_range.start, time_range.end, freq="3h")
        warnings: list[str] = []

        def _one(member: str, valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not _lead_available(lead):
                return None
            url = _file_url(member, cycle, lead)
            idx = _parse_idx(_http_range(url + ".idx", 0, "").decode())
            per_var = []
            for gv, gl, _canon, internal in selected:
                rng = None
                for i, (v, lv, sb) in enumerate(idx):
                    if v == gv and lv == gl:
                        end = idx[i + 1][2] - 1 if i + 1 < len(idx) else ""
                        rng = (sb, end)
                        break
                if rng is None:
                    continue
                ds = _open_message(_http_range(url, rng[0], rng[1]), internal)
                plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
                per_var.append(apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude"))
            if not per_var:
                return None
            return xr.merge(per_var, join="inner").expand_dims(time=[pd.Timestamp(valid)])

        member_cubes = []
        for member in self.members:
            thunks = [lambda m=member, v=v: _one(m, v) for v in valid_times]
            pieces = await self._gather_pieces(thunks, concurrency=1)
            if not pieces:
                warnings.append(f"GEFS member {member}: no data in window from cycle {cycle:%Y%m%d %Hz}")
                continue
            cube = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
            member_cubes.append(cube.expand_dims(member=[member]))

        if not member_cubes:
            raise SubsetError(
                f"No GEFS data in [{time_range.start}, {time_range.end}] from cycle "
                f"{cycle:%Y%m%d %Hz} for members {self.members}"
            )
        ds_all = xr.concat(member_cubes, dim="member") if len(member_cubes) > 1 else member_cubes[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"GEFS pgrb2s.0p25 via noaa-gefs-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; {len(member_cubes)} members; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
