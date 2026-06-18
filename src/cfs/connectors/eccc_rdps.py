# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ECCC RDPS connector — Canadian Regional Deterministic forecast (GRIB2).

The 10 km North-American regional forecast from the Meteorological Service of
Canada (MSC), the lower-resolution / longer-range sibling of ``eccc_hrdps`` on
the same MSC Datamart (same anonymous HTTPS access, same End-use Licence v2.1,
same ``today/``-only retention). See :mod:`cfs.connectors.eccc_hrdps` for the
shared access model; RDPS differs in three ways:

  * **Variable dialect** — RDPS uses the *new* MSC camelCase filename tokens
    (``AirTemp_AGL-2m``, ``SpecificHumidity_AGL-2m``, ``Pressure_Sfc``,
    ``WindU_AGL-10m``/``WindV_AGL-10m``) rather than HRDPS's NCEP short names.
  * **Resolution / range** — 10 km (``RLatLon0.09``) rotated lat/lon, hourly to
    +84 h (vs HRDPS 2.5 km to +48 h).
  * **Precipitation/radiation accumulation** (live-verified 2026-06-18 GRIB
    metadata):

      - ``DownwardShortwaveRadiationFlux-Accum_Sfc`` / ``…Longwave…`` are
        **run-total** accumulated energy (``J m-2``, stepType ``accum``) → they
        de-accumulate to a per-hour increment and ÷3600 s → ``W m-2``, exactly
        like HRDPS radiation.
      - precipitation is taken from the explicit **1-hour bucket**
        ``Precip-Accum1h_Sfc`` (mm over the preceding hour) → ÷3600 → flux, with
        **no** de-accumulation (the 1 h window is self-contained).

The instantaneous state fields (temperature, humidity, pressure, u/v wind) are
canonical SI identity. Grid is 2-D rotated lat/lon (``rotated_ll``); cfgrib
attaches 2-D ``latitude``/``longitude`` over native ``y``/``x`` dims, subset as
an index window (:func:`cfs.subset.grid2d.subset_2d_grid`).

Needs the ``cfgrib``/``eccodes`` stack (the ``forecast`` extra). Anonymous, so
live-verifiable.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import http_range, open_message
from cfs.core.config import get_settings
from cfs.core.exceptions import ConnectorError, MissingExtraError, SubsetError
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
from cfs.subset.grid2d import subset_2d_grid

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

DATAMART = "https://dd.weather.gc.ca/today"
_RDPS_DIR = "model_rdps/10km"
_RES_TOKEN = "RLatLon0.09"
_MAX_LEAD = 84
_CYCLES = (18, 12, 6, 0)  # newest-first preference order

# MSC Datamart filename token → (canonical, internal, accumulation mode).
# mode: "inst" = instantaneous identity SI; "accum" = run-total J m-2 energy
# (de-accumulate + /3600 -> W m-2); "bucket" = explicit 1-hour mm accumulation
# (/3600 -> flux, no de-accumulation).
_FIELDS: list[tuple[str, CanonicalVar, str, str]] = [
    ("AirTemp_AGL-2m", CanonicalVar.AIR_TEMPERATURE, "_t2m", "inst"),
    ("SpecificHumidity_AGL-2m", CanonicalVar.SPECIFIC_HUMIDITY, "_q2m", "inst"),
    ("Pressure_Sfc", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp", "inst"),
    ("WindU_AGL-10m", CanonicalVar.EASTWARD_WIND, "_u10", "inst"),
    ("WindV_AGL-10m", CanonicalVar.NORTHWARD_WIND, "_v10", "inst"),
    ("Precip-Accum1h_Sfc", CanonicalVar.PRECIPITATION_FLUX, "_precip", "bucket"),
    ("DownwardShortwaveRadiationFlux-Accum_Sfc", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "_dswrf", "accum"),
    ("DownwardLongwaveRadiationFlux-Accum_Sfc", CanonicalVar.LONGWAVE_RADIATION_DOWN, "_dlwrf", "accum"),
]


def _mapping(internal: str, canon: CanonicalVar, mode: str) -> VariableMapping:
    if mode == "accum":
        return VariableMapping(internal, canon, scale=1.0 / 3600.0, deaccumulate=True,
                               note="RDPS accumulated J m-2 -> per-hour increment / 3600 s -> W m-2")
    if mode == "bucket":
        return VariableMapping(internal, canon, scale=1.0 / 3600.0,
                               note="RDPS 1-hour precip accumulation (mm) / 3600 s -> kg m-2 s-1")
    return VariableMapping(internal, canon)


_MAPPINGS: list[VariableMapping] = [_mapping(i, c, m) for _t, c, i, m in _FIELDS]


def _file_url(cycle_hh: int, lead: int, run_yyyymmdd: str, token: str) -> str:
    hh = f"{cycle_hh:02d}"
    name = f"{run_yyyymmdd}T{hh}Z_MSC_RDPS_{token}_{_RES_TOKEN}_PT{lead:03d}H.grib2"
    return f"{DATAMART}/{_RDPS_DIR}/{hh}/{lead:03d}/{name}"


def _listdir(url: str) -> list[str]:
    """Return the href entries of an Apache directory index (anonymous)."""
    with urllib.request.urlopen(url, timeout=get_settings().provider_timeout_s) as r:  # noqa: S310 - https
        html = r.read().decode("utf-8", "ignore")
    return [h for h in re.findall(r'href="([^"]+)"', html) if not h.startswith("?") and h not in ("../", "/")]


def _resolve_latest_run() -> tuple[datetime, int, str]:
    """Discover the newest available RDPS run → (init_dt, cycle_hh, yyyymmdd)."""
    cycles_present = {
        int(h.strip("/"))
        for h in _listdir(f"{DATAMART}/{_RDPS_DIR}/")
        if h.strip("/").isdigit()
    }
    for hh in _CYCLES:
        if hh not in cycles_present:
            continue
        for f in _listdir(f"{DATAMART}/{_RDPS_DIR}/{hh:02d}/000/"):
            m = re.search(r"(\d{8})T(\d{2})Z_MSC_RDPS_", f)
            if m and int(m.group(2)) == hh:
                init = datetime.strptime(m.group(1), "%Y%m%d").replace(hour=hh)
                return init, hh, m.group(1)
    raise ConnectorError("eccc_rdps", "No RDPS run found on the MSC Datamart (today/)")


@register("eccc_rdps")
class ECCCRDPSConnector(BaseForcingConnector):
    slug = "eccc_rdps"
    display_name = "ECCC RDPS forecast (10 km, North America)"
    base_url = DATAMART
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:regional_10km",
                provider=self.slug,
                name="RDPS 10 km regional surface forecast",
                description=(
                    "ECCC/MSC Regional Deterministic Prediction System surface forcing "
                    "(10 km North America), downloaded per-variable from the MSC Datamart "
                    "and decoded with cfgrib. The newest available 00/06/12/18Z run "
                    "supplies the valid-time forcing (hourly to 84 h); the Datamart keeps "
                    "only recent runs (no by-date archive)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.09,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-152.0, min_lat=18.0, max_lon=-40.0, max_lat=87.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="Environment and Climate Change Canada Data Servers End-use Licence v2.1",
                citation="Environment and Climate Change Canada (ECCC/MSC), RDPS.",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "RDPS GRIB2 decoding needs the 'forecast' extra: "
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
        selected = [(tok, internal) for tok, canon, internal, _m in _FIELDS
                    if wanted is None or canon in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by RDPS")

        init, cycle_hh, run = await asyncio.to_thread(_resolve_latest_run)
        # Run-total radiation de-accumulates along time; fetch one baseline hour
        # before the window start so the first requested step's increment is right,
        # then drop it after harmonize. (The 1-hour precip bucket needs no baseline.)
        want_accum = any(m == "accum" for _t, canon, _i, m in _FIELDS
                         if (wanted is None or canon in wanted))
        first_lead = int((time_range.start - init).total_seconds() // 3600)
        baseline = time_range.start - pd.Timedelta(hours=1) if (want_accum and first_lead >= 1) else None
        window_start = baseline or time_range.start
        valid_times = pd.date_range(window_start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            lead = int((valid - init).total_seconds() // 3600)
            if not (0 <= lead <= _MAX_LEAD):
                warnings.append(
                    f"RDPS lead +{lead}h (valid {valid:%Y-%m-%dT%H}) outside the latest "
                    f"run {init:%Y%m%d %Hz} (forecast covers +0..+{_MAX_LEAD}h)"
                )
                return None
            per_var = []
            for tok, internal in selected:
                url = _file_url(cycle_hh, lead, run, tok)
                try:
                    raw = http_range(url, 0, "")  # whole single-message file
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Field absent at this lead (e.g. 1-hour precip bucket at +0 h).
                        continue
                    raise
                ds = open_message(raw, internal, label="RDPS")
                per_var.append(subset_2d_grid(ds, bbox, lat_name="latitude", lon_name="longitude"))
            if not per_var:
                return None
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No RDPS data in [{time_range.start}, {time_range.end}] from the latest "
                f"run {init:%Y%m%d %Hz} (Datamart retains only recent runs; request a "
                f"near-real-time window)"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        if baseline is not None:
            canonical = canonical.sel(time=slice(time_range.start, time_range.end))
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"ECCC RDPS 10 km via MSC Datamart (cfgrib); run {init:%Y%m%d %Hz}; "
                f"canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )
