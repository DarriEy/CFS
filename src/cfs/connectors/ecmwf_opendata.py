# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ECMWF open-data connector — IFS HRES + AIFS + ENS forecast (0.25° global, AWS GRIB2).

The major free, real-time, **global** forecast outside NOAA. Three products from
ECMWF's open data, all 0.25° global, read from the AWS Open Data mirror
``ecmwf-forecasts`` with byte-range GRIB2 access:

* ``ecmwf_opendata:ifs_0p25`` — the physics IFS HRES (``oper`` stream): to
  360 h for 00/12Z and 90 h for 06/18Z, 3-hourly to 144 h then 6-hourly.
* ``ecmwf_opendata:aifs_0p25`` — ECMWF's **machine-learning** model
  (``aifs-single``): 6-hourly to 360 h, all cycles.
* ``ecmwf_opendata:enfo_0p25`` — the **ENS ensemble** (``enfo`` stream): 50
  perturbed members on a ``member`` dimension (no control), 360 h for 00/12Z
  and 144 h for 06/18Z. Members are selected with ``config={"members": [...]}``
  (default all 50); each member de-accumulates independently. A full 50-member
  pull is many byte-range requests — restrict the member list for quick fetches.

All share the same surface forcing fields, JSON ``.index`` format, and
accumulation handling; they differ in the model/stream path segment, the step
grid, and (ENS) the ``member`` dimension. ECMWF publishes a
JSON ``.index`` sidecar (one object per message, with ``_offset``/``_length``)
rather than the NOAA colon ``.idx``; :func:`cfs.connectors.protocols.grib_idx.parse_ecmwf_index`
handles it, and the byte-range fetch + cfgrib decode + regular-grid window are
shared with the NOAA connectors.

Forecast model like ``gfs``: the most recent 00/06/12/18Z cycle at or before
the requested start supplies each valid time (``step = valid − cycle``). The
00/12Z runs go to 360 h, the 06/18Z runs to 90 h; steps are 3-hourly to 144 h
then 6-hourly. Live-probed 2026-06-15.

Variables mirror ``era5_arco`` — ``2t`` → air_temperature, ``2d`` →
dewpoint_temperature, ``10u``/``10v`` → eastward/northward_wind, ``sp`` →
surface_air_pressure (all identity SI) — plus the **accumulated** fields ``tp``
(total precipitation, m), ``ssrd`` and ``strd`` (downward solar/thermal
radiation, J m⁻²). ECMWF accumulates these from forecast start (step 0) with no
within-run reset, so the per-interval value is ``acc(step) − acc(prev_step)``
over the previous available step; precipitation is then converted m → kg m⁻²
(×1000) and divided by the interval to a flux (kg m⁻² s⁻¹), and the radiation
J m⁻² is divided by the interval to W m⁻². The **first** forecast step has no
prior step to subtract (step 0 carries no accumulation), and because the
accumulation starts at t=0 its increment is simply ``acc(step)`` — so the first
step's precipitation/radiation are kept, not dropped.

ECMWF open-data is **CC-BY-4.0** (attribution), unlike the NOAA public-domain
sources. Needs the ``forecast`` extra (cfgrib/eccodes). Anonymous on the AWS
mirror, so live-verifiable.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import (
    cycle_for,
    http_range,
    parse_ecmwf_index,
    read_message,
)
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

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

GRIB_BASE = "https://ecmwf-forecasts.s3.amazonaws.com"

# Instantaneous surface fields (ECMWF param → canonical), identity SI.
_INST_FIELDS: list[tuple[str, CanonicalVar]] = [
    ("2t", CanonicalVar.AIR_TEMPERATURE),
    ("2d", CanonicalVar.DEWPOINT_TEMPERATURE),
    ("10u", CanonicalVar.EASTWARD_WIND),
    ("10v", CanonicalVar.NORTHWARD_WIND),
    ("sp", CanonicalVar.SURFACE_AIR_PRESSURE),
]
# Accumulated-from-step-0 fields: (param, canonical, scale). De-accumulated to a
# per-interval increment, then ×scale and ÷interval_seconds. tp is metres of water
# (×1000 → kg m-2); ssrd/strd are J m-2 (scale 1 → W m-2 after ÷ seconds).
_ACCUM_FIELDS: list[tuple[str, CanonicalVar, float]] = [
    ("tp", CanonicalVar.PRECIPITATION_FLUX, 1000.0),
    ("ssrd", CanonicalVar.SHORTWAVE_RADIATION_DOWN, 1.0),
    ("strd", CanonicalVar.LONGWAVE_RADIATION_DOWN, 1.0),
]
_MAPPINGS: list[VariableMapping] = (
    [VariableMapping(p, canon) for p, canon in _INST_FIELDS]
    + [VariableMapping(p, canon) for p, canon, _s in _ACCUM_FIELDS]
)

_STEP_HOURLY_MAX = 144  # IFS/ENS: 3-hourly to 144 h, then 6-hourly

# Default ENS ensemble: the 50 perturbed members (enfo ships no control member).
_ALL_ENS_MEMBERS = [str(i) for i in range(1, 51)]

# Products keyed by id suffix. `model`/`stream`/`suffix` build the archive path;
# `spacing` selects the step grid ("ifs" = 3-hourly to 144 h then 6-hourly,
# "aifs" = 6-hourly); `short_max` is the 06/18Z horizon (360 h is the 00/12Z
# horizon for all). `ensemble` toggles the member dimension.
_PRODUCTS: dict[str, dict] = {
    "ifs_0p25": {
        "model": "ifs", "stream": "oper", "suffix": "oper-fc",
        "spacing": "ifs", "freq": "3h", "short_max": 90, "ensemble": False,
        "name": "ECMWF IFS HRES 0.25° forecast (global)",
        "blurb": "ECMWF open-data IFS HRES (oper) surface forcing",
    },
    "aifs_0p25": {
        "model": "aifs-single", "stream": "oper", "suffix": "oper-fc",
        "spacing": "aifs", "freq": "6h", "short_max": 360, "ensemble": False,
        "name": "ECMWF AIFS 0.25° ML forecast (global)",
        "blurb": "ECMWF open-data AIFS-single (oper) surface forcing — ECMWF's "
                 "machine-learning model",
    },
    "enfo_0p25": {
        "model": "ifs", "stream": "enfo", "suffix": "enfo-ef",
        "spacing": "ifs", "freq": "3h", "short_max": 144, "ensemble": True,
        "name": "ECMWF ENS 0.25° ensemble forecast (global, 50 members)",
        "blurb": "ECMWF open-data ENS (enfo) surface forcing — 50 perturbed "
                 "members on a `member` dimension",
    },
}


def _max_step(cycle_hour: int, short_max: int) -> int:
    return 360 if cycle_hour in (0, 12) else short_max


def _step_available(step: int, cycle_hour: int, spacing: str, short_max: int) -> bool:
    if not (0 <= step <= _max_step(cycle_hour, short_max)):
        return False
    if spacing == "aifs":
        return step % 6 == 0
    return step % 3 == 0 if step <= _STEP_HOURLY_MAX else step % 6 == 0


def _prev_step(step: int, spacing: str) -> int:
    """Previous available step (AIFS: 6 h; IFS/ENS: 3 h to 144 h, 6 h after)."""
    if spacing == "aifs":
        return step - 6
    return step - 3 if step <= _STEP_HOURLY_MAX else step - 6


def _file_url(model: str, stream: str, suffix: str, cycle: datetime, step: int) -> str:
    d, h = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"{GRIB_BASE}/{d}/{h}z/{model}/0p25/{stream}/{d}{h}0000-{step}h-{suffix}.grib2"


def _index_url(model: str, stream: str, suffix: str, cycle: datetime, step: int) -> str:
    # ECMWF's .index REPLACES the .grib2 suffix (unlike NOAA's appended .idx).
    return _file_url(model, stream, suffix, cycle, step)[: -len(".grib2")] + ".index"


def _find(records, param: str, levtype: str = "sfc", number: str | None = None):
    """First ``(start, end)`` byte range for ``param``/``levtype``/``number``, or ``None``.

    ``number`` is the ensemble member for the enfo stream (``None`` for the
    deterministic oper records, which carry no member).
    """
    for p, lt, _st, num, off, length in records:
        if p == param and lt == levtype and num == number:
            return off, off + length - 1
    return None


@register("ecmwf_opendata")
class ECMWFOpenDataConnector(BaseForcingConnector):
    slug = "ecmwf_opendata"
    display_name = "ECMWF open-data IFS HRES + AIFS forecast (0.25°, global)"
    base_url = GRIB_BASE
    protocol = "http"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # ENS (enfo) member selection; ignored by the deterministic products.
        self.members = [str(m) for m in self.config.get("members", _ALL_ENS_MEMBERS)]

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:{suffix}",
                provider=self.slug,
                name=spec["name"],
                description=(
                    f"{spec['blurb']}, byte-range read from the GRIB2 files on the "
                    "ecmwf-forecasts AWS mirror. The most recent 00/06/12/18Z cycle "
                    "at/before the requested start supplies the valid-time forcing. "
                    "Precipitation and radiation are de-accumulated to fluxes."
                    + (" Returns a `member` dimension (50 perturbed members; "
                       "restrict via config={'members': [...]})." if spec["ensemble"] else "")
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(
                    resolution=TemporalResolution.THREE_HOURLY
                    if spec["spacing"] == "ifs" else TemporalResolution.SIX_HOURLY
                ),
                protocol=Protocol.REST,
                license="ECMWF open data (CC-BY-4.0; attribution required)",
                citation="ECMWF Integrated Forecasting System (IFS) / AIFS open data.",
            )
            for suffix, spec in _PRODUCTS.items()
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "ECMWF open-data GRIB2 decoding needs the 'forecast' extra: "
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
        spec = _PRODUCTS[product_id.split(":", 1)[1]]
        model, stream, suffix = spec["model"], spec["stream"], spec["suffix"]
        spacing, short_max, ensemble = spec["spacing"], spec["short_max"], spec["ensemble"]
        settings = get_settings()
        self._guard_area(bbox, settings)
        self._require_cfgrib()

        wanted = set(variables) if variables else None
        selected_inst = [f for f in _INST_FIELDS if wanted is None or f[1] in wanted]
        selected_accum = [f for f in _ACCUM_FIELDS if wanted is None or f[1] in wanted]
        if not selected_inst and not selected_accum:
            raise SubsetError("None of the requested variables are offered by ECMWF open-data")
        if ensemble and not self.members:
            raise SubsetError("ECMWF ENS member list is empty")

        cycle = cycle_for(time_range.start, step_h=6)
        cyc_h = cycle.hour
        valid_times = pd.date_range(time_range.start, time_range.end, freq=spec["freq"])
        warnings: list[str] = []

        def _piece(member, valid):
            step = int((valid - cycle).total_seconds() // 3600)
            if not _step_available(step, cyc_h, spacing, short_max):
                warnings.append(
                    f"ECMWF step {step}h (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz} (max {_max_step(cyc_h, short_max)}h)"
                )
                return None
            url = _file_url(model, stream, suffix, cycle, step)
            recs = parse_ecmwf_index(
                http_range(_index_url(model, stream, suffix, cycle, step), 0, "").decode())
            per_var = []
            for param, _canon in selected_inst:
                rng = _find(recs, param, number=member)
                if rng is not None:
                    per_var.append(read_message(url, rng[0], rng[1], param, bbox, label="ECMWF"))
            # Accumulated fields de-accumulate against the previous step (same member).
            # ECMWF accumulates from t=0, so the FIRST forecast step's increment is
            # acc(step) itself — step 0 carries no accumulation baseline to subtract.
            if selected_accum and step > 0:
                prev = _prev_step(step, spacing)
                interval = (step - prev) * 3600.0
                precs = None
                if prev > 0:
                    precs = parse_ecmwf_index(
                        http_range(_index_url(model, stream, suffix, cycle, prev), 0, "").decode())
                for param, _canon, scale in selected_accum:
                    cr = _find(recs, param, number=member)
                    if cr is None:
                        continue
                    cur = read_message(url, cr[0], cr[1], param, bbox, label="ECMWF")
                    if precs is not None:  # subtract the previous step's run-total
                        pr = _find(precs, param, number=member)
                        if pr is None:
                            continue
                        cur = cur - read_message(
                            _file_url(model, stream, suffix, cycle, prev),
                            pr[0], pr[1], param, bbox, label="ECMWF")
                    per_var.append((cur * (scale / interval)).clip(min=0))
            if not per_var:
                return None
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        async def _member_cube(member):
            pieces = await self._gather_pieces([partial(_piece, member, v) for v in valid_times])
            if not pieces:
                return None
            return xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        if ensemble:
            cubes = []
            for member in self.members:
                cube = await _member_cube(member)
                if cube is None:
                    warnings.append(f"ECMWF ENS member {member}: no data in window")
                    continue
                cubes.append(cube.expand_dims(member=[int(member)]))
            if not cubes:
                raise SubsetError(
                    f"No ECMWF ENS data in [{time_range.start}, {time_range.end}] from cycle "
                    f"{cycle:%Y%m%d %Hz} for members {self.members}"
                )
            ds_all = xr.concat(cubes, dim="member") if len(cubes) > 1 else cubes[0]
        else:
            ds_all = await _member_cube(None)
            if ds_all is None:
                raise SubsetError(
                    f"No ECMWF open-data in [{time_range.start}, {time_range.end}] from cycle "
                    f"{cycle:%Y%m%d %Hz} (the open-data mirror retains roughly the last few days)"
                )

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        member_note = f"{len(ds_all['member'])} members; " if ensemble else ""
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"ECMWF {model}/{stream} 0p25 via ecmwf-forecasts AWS byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; {member_note}tp/ssrd/strd de-accumulated; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
