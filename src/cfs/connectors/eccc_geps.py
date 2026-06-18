# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ECCC GEPS connector — Canadian Global Ensemble forecast (GRIB2, ensemble mean).

The 0.5° global Global Ensemble Prediction System from the Meteorological Service
of Canada (MSC), read anonymously over HTTPS from the MSC Datamart — the ensemble
sibling of the ``eccc_hrdps`` / ``eccc_rdps`` deterministic connectors (same
provider, same End-use Licence v2.1, same latest-run-only access model).

**Access model** (identical to HRDPS): the Datamart keeps only recent runs under
a moving ``today/`` alias, so this connector discovers the newest available GEPS
run (its init date/hour live in the filenames,
``CMC_geps-raw_…_YYYYMMDDHH_P{lead}_allmbrs.grib2``) and downloads the per-field
GRIB2 files whole. GEPS runs the **00 and 12Z** cycles, with leads 3-hourly to
192 h then 6-hourly (capped here at 384 h; the Mon/Thu 00Z extension to 936 h is
not exposed).

**Ensemble reduction.** Each ``_allmbrs`` file packs the control (``cf``) and 20
perturbed (``pf``) members; cfgrib cannot open both at once (``multiple values
for key 'dataType'``), so this connector reads the 20 perturbed members
(``filter_by_keys={'dataType': 'pf'}``) and returns their **ensemble mean** — the
standard deterministic estimate from an ensemble. (Per-member access is a
possible future config option.)

**Variables.** Live-verified 2026-06-18 GRIB metadata: ``TMP`` 2 m, ``SPFH`` 2 m,
``PRES`` surface, ``UGRD``/``VGRD`` 10 m are instantaneous canonical SI
(identity); ``DSWRF``/``DLWRF`` surface (J m⁻², accum) and ``APCP`` surface (mm,
accum) are accumulated since run start. Because the lead step changes (3 h → 6 h),
the accumulated fields are de-accumulated to a per-step increment and divided by
the **actual seconds between consecutive leads** (not a fixed scale), giving
W m⁻² and kg m⁻² s⁻¹. The grid is regular 0.5° lat/lon on 0–360 longitude
(normalized on subset), so the 1-D bbox path is used.

Needs the ``cfgrib``/``eccodes`` stack (the ``forecast`` extra). Anonymous, so
live-verifiable.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import http_range
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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

DATAMART = "https://dd.weather.gc.ca/today"
_GEPS_DIR = "ensemble/geps/grib2/raw"
_GRID_TOKEN = "latlon0p5x0p5"
_MAX_LEAD = 384
_CYCLES = (12, 0)  # newest-first preference order (GEPS runs 00/12Z only)

# MSC Datamart "{VAR}_{LEVEL}" token → (canonical, internal, accumulated?).
# Instantaneous fields are canonical SI (identity); DSWRF/DLWRF (J m-2) and APCP
# (mm) are accumulated since run start → de-accumulated to a per-step flux.
_FIELDS: list[tuple[str, CanonicalVar, str, bool]] = [
    ("TMP_TGL_2m", CanonicalVar.AIR_TEMPERATURE, "_t2m", False),
    ("SPFH_TGL_2", CanonicalVar.SPECIFIC_HUMIDITY, "_q2m", False),
    ("PRES_SFC_0", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp", False),
    ("UGRD_TGL_10m", CanonicalVar.EASTWARD_WIND, "_u10", False),
    ("VGRD_TGL_10m", CanonicalVar.NORTHWARD_WIND, "_v10", False),
    ("DSWRF_SFC_0", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "_dswrf", True),
    ("DLWRF_SFC_0", CanonicalVar.LONGWAVE_RADIATION_DOWN, "_dlwrf", True),
    ("APCP_SFC_0", CanonicalVar.PRECIPITATION_FLUX, "_apcp", True),
]
# All mappings are identity: instantaneous fields are already canonical SI, and
# the accumulated fields are converted to flux (W m-2 / kg m-2 s-1) in-connector
# before harmonization (the per-step seconds vary with the 3h→6h lead step).
_MAPPINGS: list[VariableMapping] = [VariableMapping(internal, canon) for _t, canon, internal, _a in _FIELDS]
_ACCUM_INTERNALS = frozenset(internal for _t, _c, internal, accum in _FIELDS if accum)


def _lead_grid(max_lead: int = _MAX_LEAD) -> list[int]:
    """GEPS forecast leads: 3-hourly to 192 h, then 6-hourly to ``max_lead``."""
    return list(range(0, 193, 3)) + list(range(198, max_lead + 1, 6))


def _file_url(cycle_hh: int, lead: int, run_yyyymmddhh: str, token: str) -> str:
    hh = f"{cycle_hh:02d}"
    name = f"CMC_geps-raw_{token}_{_GRID_TOKEN}_{run_yyyymmddhh}_P{lead:03d}_allmbrs.grib2"
    return f"{DATAMART}/{_GEPS_DIR}/{hh}/{lead:03d}/{name}"


def _listdir(url: str) -> list[str]:
    """Return the href entries of an Apache directory index (anonymous)."""
    with urllib.request.urlopen(url, timeout=get_settings().provider_timeout_s) as r:  # noqa: S310 - https
        html = r.read().decode("utf-8", "ignore")
    return [h for h in re.findall(r'href="([^"]+)"', html) if not h.startswith("?") and h not in ("../", "/")]


def _resolve_latest_run() -> tuple[datetime, int, str]:
    """Discover the newest available GEPS run → (init_dt, cycle_hh, yyyymmddhh).

    Mirrors the HRDPS resolver: the ``today/`` alias holds whatever cycles have
    completed; the run init (date + hour) lives in the ``…_YYYYMMDDHH_P000_…``
    filenames. Picks the newest present cycle and reads its init from a P000 file.
    """
    cycles_present = {
        int(h.strip("/"))
        for h in _listdir(f"{DATAMART}/{_GEPS_DIR}/")
        if h.strip("/").isdigit()
    }
    for hh in _CYCLES:
        if hh not in cycles_present:
            continue
        for f in _listdir(f"{DATAMART}/{_GEPS_DIR}/{hh:02d}/000/"):
            m = re.search(rf"_{_GRID_TOKEN}_(\d{{10}})_P000_", f)
            if m and int(m.group(1)[-2:]) == hh:
                init = datetime.strptime(m.group(1), "%Y%m%d%H")
                return init, hh, m.group(1)
    raise ConnectorError("eccc_geps", "No GEPS run found on the MSC Datamart (today/)")


def _open_member_mean(raw: bytes, internal: str) -> xr.Dataset:
    """Decode one GEPS ``_allmbrs`` GRIB2 message → ensemble-mean single-var Dataset.

    Reads the 20 perturbed members (``dataType='pf'``; the control ``cf`` is a
    separate dataType cfgrib cannot co-open), averages over ``number``, renames to
    ``internal``, and drops every coord but ``latitude``/``longitude``.
    """
    import xarray as xr

    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        ds = xr.open_dataset(
            path, engine="cfgrib",
            backend_kwargs={"indexpath": "", "filter_by_keys": {"dataType": "pf"}},
        ).load()
    finally:
        os.unlink(path)
    data_vars = list(ds.data_vars)
    if not data_vars:
        raise SubsetError(f"GEPS message for {internal} decoded no variable")
    da = ds[data_vars[0]].mean("number")
    da.name = internal
    out = da.to_dataset()
    drop = [c for c in out.coords if c not in ("latitude", "longitude")]
    return out.drop_vars(drop, errors="ignore")


@register("eccc_geps")
class ECCCGEPSConnector(BaseForcingConnector):
    slug = "eccc_geps"
    display_name = "ECCC GEPS ensemble-mean forecast (0.5°, global)"
    base_url = DATAMART
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:global_0p5_mean",
                provider=self.slug,
                name="GEPS 0.5° global ensemble-mean surface forecast",
                description=(
                    "ECCC/MSC Global Ensemble Prediction System surface forcing "
                    "(0.5° global, 20-member perturbed ensemble mean), downloaded "
                    "per-variable from the MSC Datamart and decoded with cfgrib. The "
                    "newest 00/12Z run supplies the valid-time forcing (3-hourly to "
                    "192 h, 6-hourly to 384 h); the Datamart keeps only recent runs "
                    "(no by-date archive)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.5,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.THREE_HOURLY),
                protocol=Protocol.REST,
                license="Environment and Climate Change Canada Data Servers End-use Licence v2.1",
                citation="Environment and Climate Change Canada (ECCC/MSC), GEPS.",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "GEPS GRIB2 decoding needs the 'forecast' extra: "
                "pip install -e '.[forecast]' (cfgrib + eccodes)"
            ) from e

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import numpy as np
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)
        self._require_cfgrib()

        wanted = set(variables) if variables else None
        selected = [(tok, internal) for tok, canon, internal, _a in _FIELDS
                    if wanted is None or canon in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by GEPS")
        want_accum = any(internal in _ACCUM_INTERNALS for _t, internal in selected)

        init, cycle_hh, run = await asyncio.to_thread(_resolve_latest_run)

        # Leads on the GEPS grid whose valid time falls in the window; prepend the
        # previous grid lead as a baseline so the accumulated fields' first
        # in-window increment is correct (it is sliced off after harmonization).
        grid = _lead_grid()
        in_window = [L for L in grid if time_range.start <= init + timedelta(hours=L) <= time_range.end]
        if not in_window:
            raise SubsetError(
                f"No GEPS lead valid in [{time_range.start}, {time_range.end}] from the "
                f"latest run {init:%Y%m%d %Hz} (3-hourly to 192 h, then 6-hourly to {_MAX_LEAD} h)"
            )
        fetch_leads = list(in_window)
        if want_accum and in_window[0] > 0:
            i = grid.index(in_window[0])
            if i > 0:
                fetch_leads = [grid[i - 1], *in_window]
        warnings: list[str] = []

        def _piece(lead: int):
            valid = init + timedelta(hours=lead)
            per_var = []
            for tok, internal in selected:
                url = _file_url(cycle_hh, lead, run, tok)
                try:
                    raw = http_range(url, 0, "")  # whole single-message file
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Accumulated fields are absent at lead 0 (nothing to accumulate).
                        continue
                    raise
                ds = _open_member_mean(raw, internal)
                plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
                per_var.append(apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude"))
            if not per_var:
                return None
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, L) for L in fetch_leads])
        if not pieces:
            raise SubsetError(
                f"No GEPS data in [{time_range.start}, {time_range.end}] from the latest "
                f"run {init:%Y%m%d %Hz} (Datamart retains only recent runs)"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        # De-accumulate the energy/precip fields to a per-step flux, dividing by the
        # ACTUAL seconds between consecutive leads (the lead step changes 3h→6h).
        if want_accum and ds_all.sizes.get("time", 0) >= 2:
            times = ds_all["time"].values
            dt_s = np.full(times.shape, np.nan)
            dt_s[1:] = np.diff(times) / np.timedelta64(1, "s")
            dt_da = xr.DataArray(dt_s, dims="time", coords={"time": ds_all["time"]})
            for internal in _ACCUM_INTERNALS:
                if internal not in ds_all:
                    continue
                da = ds_all[internal]
                inc = da - da.shift(time=1)
                inc = xr.where(inc < 0, da, inc)  # guard a (rare) reset
                ds_all[internal] = (inc / dt_da).clip(min=0)

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        # Drop the baseline lead (valid time < window start) seeded for de-accum.
        canonical = canonical.sel(time=slice(time_range.start, time_range.end))
        if canonical.sizes.get("time", 0) == 0:
            raise SubsetError(f"No GEPS data in [{time_range.start}, {time_range.end}]")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"ECCC GEPS 0.5° 20-member ensemble mean via MSC Datamart (cfgrib); "
                f"run {init:%Y%m%d %Hz}; accumulated fields de-accumulated to flux; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
