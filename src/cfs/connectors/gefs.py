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

Instantaneous fields are identity SI — ``TMP`` 2 m → air_temperature, ``PRES``
surface → surface_air_pressure, ``UGRD``/``VGRD`` 10 m → eastward/northward_wind.

Precipitation and radiation need **bucket-aware de-accumulation**. GEFS-select
ships them as 6-hour-bucket quantities at 3-hourly leads: ``APCP`` *accumulates*
(``0-3``, ``0-6``, ``6-9``, ``6-12``, …) and ``DSWRF``/``DLWRF`` *average* over the
same buckets. The generic reset-by-sign de-accumulator is unsafe here — a fresh
bucket with heavier precip than the previous bucket's total keeps the diff positive
and the reset goes undetected — so the bucket boundary is taken deterministically
from the lead hour instead. With all leads a multiple of 3, a lead is either the
**first** half of its bucket (``lead % 6 == 3``: ``0-3``, ``6-9`` … — already the
per-3 h quantity) or the **second** (``lead % 6 == 0``: ``0-6``, ``6-12`` … — the
full bucket), where the per-interval ``[lead-3, lead]`` value is recovered from the
first-half field of the *same* bucket: ``cur - prev`` for accumulations, ``2*cur -
prev`` for averages. ``APCP`` (kg m⁻² over the 3 h interval) is then divided by the
interval to a flux (kg m⁻² s⁻¹, matching ``gfs``'s ``PRATE``); de-averaged
``DSWRF``/``DLWRF`` are already W m⁻² (identity). All are absent at f000 (analysis).

GEFS-select ships relative humidity, not specific humidity, so specific_humidity is
*derived* from 2 m ``RH`` + 2 m temperature + surface pressure (Bolton 1980, via
:mod:`cfs.derive.humidity`). ``RH`` is fetched only as a derivation input — never
exposed raw — and only when specific_humidity is requested.

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
from functools import partial
from typing import TYPE_CHECKING

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
from cfs.derive.humidity import specific_humidity_from_rh
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"

# Default ensemble: control + 30 perturbations.
_ALL_MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]

# Instantaneous surface fields (identity SI); .idx level strings matched exactly.
# (var, level, canonical, internal-name)
_VARS = [
    ("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE, "_t2m"),
    ("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp"),
    ("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND, "_u10"),
    ("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND, "_v10"),
]

# Bucket-averaged/accumulated surface fields; de-bucketed per-interval before
# unit conversion. (var, level, canonical, internal-name, kind) where kind is
# "acc" (accumulation → cur-prev) or "ave" (average → 2*cur-prev).
_INTERVAL_S = 3 * 3600  # 3-hourly leads
_FLUX_VARS = [
    ("APCP", "surface", CanonicalVar.PRECIPITATION_FLUX, "_apcp", "acc"),
    ("DSWRF", "surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "_dswrf", "ave"),
    ("DLWRF", "surface", CanonicalVar.LONGWAVE_RADIATION_DOWN, "_dlwrf", "ave"),
]

_MAPPINGS: list[VariableMapping] = [
    VariableMapping(internal, canon) for _, _, canon, internal in _VARS
] + [
    # APCP is de-bucketed to a kg m-2 increment over the 3 h interval here, then
    # divided to a flux; radiation is already a mean W m-2 (identity).
    VariableMapping("_apcp", CanonicalVar.PRECIPITATION_FLUX, scale=1.0 / _INTERVAL_S),
    VariableMapping("_dswrf", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("_dlwrf", CanonicalVar.LONGWAVE_RADIATION_DOWN),
]

# Specific humidity is not shipped — GEFS-select carries 2 m relative humidity, so
# q is *derived* from RH + 2 m temperature + surface pressure (Bolton 1980). RH is
# an instantaneous field fetched only as a derivation input (never exposed raw); T
# and P are reused from the instantaneous set above. (grib_var, grib_level, internal)
_DERIVED_Q = "_q2m"
_Q_INPUTS = [
    ("RH", "2 m above ground", "_rh2m"),
    ("TMP", "2 m above ground", "_t2m"),
    ("PRES", "surface", "_sp"),
]

# pgrb2sp25 (the 0.25° select product) is produced 3-hourly to f240 only — f243+
# do not exist (the coarser products run longer). Live-confirmed against the S3 idx.
_MAX_LEAD = 240


def _lead_available(lead: int) -> bool:
    return 0 <= lead <= _MAX_LEAD and lead % 3 == 0


def _file_url(member: str, cycle, lead: int) -> str:
    d = cycle.strftime("%Y%m%d")
    h = cycle.strftime("%H")
    return f"{S3_BASE}/gefs.{d}/{h}/atmos/pgrb2sp25/{member}.t{h}z.pgrb2s.0p25.f{lead:03d}"


def _http_range(url: str, start: int, end: int | str) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})  # noqa: S310 - https S3
    with urllib.request.urlopen(req, timeout=get_settings().provider_timeout_s) as r:
        data: bytes = r.read()
    return data


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


def _byte_range(idx, grib_var: str, grib_level: str):
    """Locate (start, end) byte range of one (var, level) message in a parsed idx."""
    for i, (v, lv, sb) in enumerate(idx):
        if v == grib_var and lv == grib_level:
            return sb, (idx[i + 1][2] - 1 if i + 1 < len(idx) else "")
    return None


def _read_field(url: str, idx, grib_var: str, grib_level: str, internal: str, bbox):
    """Byte-range fetch + decode + bbox-subset one (var, level) field, or None."""
    rng = _byte_range(idx, grib_var, grib_level)
    if rng is None:
        return None
    ds = _open_message(_http_range(url, rng[0], rng[1]), internal)
    plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
    return apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude")


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
                    "perturbations). Instantaneous fields plus bucket-de-accumulated "
                    "precipitation_flux and down shortwave/longwave radiation, and "
                    "specific_humidity derived from 2 m RH + temperature + pressure."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ] + [
                    ProductVariable(
                        canonical=CanonicalVar.SPECIFIC_HUMIDITY, source_name=_DERIVED_Q
                    )
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
    ) -> tuple[xr.Dataset, FetchResult]:
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)
        self._require_cfgrib()

        wanted = set(variables) if variables else None
        selected_inst = [v for v in _VARS if wanted is None or v[2] in wanted]
        selected_flux = [v for v in _FLUX_VARS if wanted is None or v[2] in wanted]
        want_q = wanted is None or CanonicalVar.SPECIFIC_HUMIDITY in wanted

        # Instantaneous (var, level, internal) fields to byte-range fetch: the
        # exposed identity fields, plus RH/T/P when specific humidity is derived
        # (T/P may already be selected — dedup by internal name).
        inst_specs = [(gv, gl, internal) for gv, gl, _c, internal in selected_inst]
        if want_q:
            have = {internal for _, _, internal in inst_specs}
            inst_specs += [s for s in _Q_INPUTS if s[2] not in have]

        if not inst_specs and not selected_flux:
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
            per_var = [
                f for gv, gl, internal in inst_specs
                if (f := _read_field(url, idx, gv, gl, internal, bbox)) is not None
            ]
            # Precip/radiation are bucket quantities (none at f000): de-bucket the
            # per-interval value, fetching the bucket's first-half field when the
            # lead is a second-half step (lead % 6 == 0).
            if selected_flux and lead > 0:
                second_half = lead % 6 == 0
                idx_prev = url_prev = None
                if second_half:
                    url_prev = _file_url(member, cycle, lead - 3)
                    idx_prev = _parse_idx(_http_range(url_prev + ".idx", 0, "").decode())
                for gv, gl, _c, internal, kind in selected_flux:
                    cur = _read_field(url, idx, gv, gl, internal, bbox)
                    if cur is None:
                        continue
                    if url_prev is not None and idx_prev is not None:
                        prev = _read_field(url_prev, idx_prev, gv, gl, internal, bbox)
                        if prev is None:
                            continue  # cannot de-bucket without the first half
                        cur = cur - prev if kind == "acc" else 2 * cur - prev
                    per_var.append(cur.clip(min=0))
            if not per_var:
                return None
            return xr.merge(per_var, join="inner").expand_dims(time=[pd.Timestamp(valid)])

        member_cubes = []
        for member in self.members:
            thunks = [partial(_one, member, v) for v in valid_times]
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

        # Derive specific humidity from RH + 2 m T + surface pressure (the raw RH
        # input is then dropped by harmonize, which keeps only mapped variables).
        mappings = _MAPPINGS
        if want_q:
            if all(n in ds_all.data_vars for n in ("_rh2m", "_t2m", "_sp")):
                q = specific_humidity_from_rh(ds_all["_rh2m"], ds_all["_t2m"], ds_all["_sp"])
                ds_all = ds_all.assign({_DERIVED_Q: q})
                mappings = [*_MAPPINGS, VariableMapping(_DERIVED_Q, CanonicalVar.SPECIFIC_HUMIDITY)]
            else:
                warnings.append(
                    "GEFS specific_humidity not derived: missing one of RH/T/pressure "
                    "in the fetched messages"
                )

        canonical = harmonize(ds_all, mappings, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"GEFS pgrb2s.0p25 via noaa-gefs-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; {len(member_cubes)} members; "
                f"precip/radiation bucket-de-accumulated to 3 h intervals; "
                f"specific_humidity derived from RH (Bolton 1980) when requested; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
