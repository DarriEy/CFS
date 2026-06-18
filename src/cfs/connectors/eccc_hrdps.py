# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""ECCC HRDPS connector — Canadian High-Resolution Deterministic forecast (GRIB2).

The 2.5 km continental forecast from the Meteorological Service of Canada (MSC),
read anonymously over HTTPS from the MSC Datamart. The natural companion to the
``rdrs`` reanalysis already in CFS: same provider, same End-use Licence v2.1.

**Access model.** The Datamart serves only the *latest* model runs (≈24 h
retention) under a moving ``today/`` alias — there is no by-date archive — and
each GRIB2 file holds a single field+level. So unlike the NOAA forecast
connectors (which address a historical cycle by date and byte-range a fat
multi-field file via its ``.idx``), this connector:

  1. discovers the newest available run under
     ``…/model_hrdps/continental/2.5km/{HH}/`` (its init date/hour live in the
     filenames, e.g. ``20260618T00Z_MSC_HRDPS_…``),
  2. for each requested valid hour computes ``lead = valid − init`` (HRDPS runs
     to 48 h, hourly), and
  3. downloads the small per-variable GRIB2 whole (no ``.idx`` sidecar exists)
     and decodes it with cfgrib.

All eight forcing fields are published as **instantaneous** surface fields in
canonical SI — ``TMP``/``SPFH`` 2 m, ``PRES`` surface, ``UGRD``/``VGRD`` 10 m,
``PRATE`` surface (an instantaneous rate, so no de-accumulation), and
``DSWRF``/``DLWRF`` surface — so every mapping is identity. The grid is a 2.5 km
**rotated lat/lon** (``rotated_ll``); cfgrib attaches 2-D ``latitude``/
``longitude`` over native ``y``/``x`` dims, subset as an index window
(:func:`cfs.subset.grid2d.subset_2d_grid`), exactly like the projected NOAA
connectors (HRRR/NAM).

Needs the ``cfgrib``/``eccodes`` stack (the ``forecast`` extra). Anonymous, so
live-verifiable. RDPS (10 km) and GEPS (ensemble) are siblings on the same
Datamart with different variable dialects and accumulated radiation/precip —
follow-up connectors.
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
_HRDPS_DIR = "model_hrdps/continental/2.5km"
_RES_TOKEN = "RLatLon0.0225"
_MAX_LEAD = 48
_CYCLES = (18, 12, 6, 0)  # newest-first preference order

# MSC Datamart filename token (HRDPS uses NCEP-style short names) → canonical.
# Wind components live in separate files (no combined U/V message). Live-verified
# 2026-06-18 GRIB metadata: TMP/SPFH/PRES/UGRD/VGRD and PRATE are instantaneous
# canonical SI (identity); DSWRF/DLWRF are accumulated-since-run-start energy
# (J m-2, stepType=accum), so they de-accumulate to a per-hour increment and
# divide by 3600 s → W m-2. ``accum`` flags the energy-accumulation fields.
_FIELDS: list[tuple[str, CanonicalVar, str, bool]] = [
    ("TMP_AGL-2m", CanonicalVar.AIR_TEMPERATURE, "_t2m", False),
    ("SPFH_AGL-2m", CanonicalVar.SPECIFIC_HUMIDITY, "_q2m", False),
    ("PRES_Sfc", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp", False),
    ("UGRD_AGL-10m", CanonicalVar.EASTWARD_WIND, "_u10", False),
    ("VGRD_AGL-10m", CanonicalVar.NORTHWARD_WIND, "_v10", False),
    ("PRATE_Sfc", CanonicalVar.PRECIPITATION_FLUX, "_prate", False),
    ("DSWRF_Sfc", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "_dswrf", True),
    ("DLWRF_Sfc", CanonicalVar.LONGWAVE_RADIATION_DOWN, "_dlwrf", True),
]
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        internal, canon,
        scale=(1.0 / 3600.0) if accum else 1.0,
        deaccumulate=accum,
        note=("HRDPS accumulated J m-2 -> per-hour increment / 3600 s -> W m-2" if accum else ""),
    )
    for _t, canon, internal, accum in _FIELDS
]


def _file_url(cycle_hh: int, lead: int, run_yyyymmdd: str, token: str) -> str:
    hh = f"{cycle_hh:02d}"
    name = f"{run_yyyymmdd}T{hh}Z_MSC_HRDPS_{token}_{_RES_TOKEN}_PT{lead:03d}H.grib2"
    return f"{DATAMART}/{_HRDPS_DIR}/{hh}/{lead:03d}/{name}"


def _listdir(url: str) -> list[str]:
    """Return the href entries of an Apache directory index (anonymous)."""
    with urllib.request.urlopen(url, timeout=get_settings().provider_timeout_s) as r:  # noqa: S310 - https
        html = r.read().decode("utf-8", "ignore")
    return [h for h in re.findall(r'href="([^"]+)"', html) if not h.startswith("?") and h not in ("../", "/")]


def _resolve_latest_run() -> tuple[datetime, int, str]:
    """Discover the newest available HRDPS run → (init_dt, cycle_hh, yyyymmdd).

    The Datamart ``today/`` alias holds whatever cycles have completed; the run
    date is embedded in the filenames (it can lag the server's calendar date).
    Picks the newest present cycle and reads its init date from a lead-000 file.
    """
    cycles_present = {
        int(h.strip("/"))
        for h in _listdir(f"{DATAMART}/{_HRDPS_DIR}/")
        if h.strip("/").isdigit()
    }
    for hh in _CYCLES:
        if hh not in cycles_present:
            continue
        files = _listdir(f"{DATAMART}/{_HRDPS_DIR}/{hh:02d}/000/")
        for f in files:
            m = re.search(r"(\d{8})T(\d{2})Z_MSC_HRDPS_", f)
            if m and int(m.group(2)) == hh:
                init = datetime.strptime(m.group(1), "%Y%m%d").replace(hour=hh)
                return init, hh, m.group(1)
    raise ConnectorError("eccc_hrdps", "No HRDPS run found on the MSC Datamart (today/)")


@register("eccc_hrdps")
class ECCCHRDPSConnector(BaseForcingConnector):
    slug = "eccc_hrdps"
    display_name = "ECCC HRDPS forecast (2.5 km, continental North America)"
    base_url = DATAMART
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:continental_2p5km",
                provider=self.slug,
                name="HRDPS 2.5 km continental surface forecast",
                description=(
                    "ECCC/MSC High-Resolution Deterministic Prediction System surface "
                    "forcing (2.5 km continental North America), downloaded per-variable "
                    "from the MSC Datamart and decoded with cfgrib. The newest available "
                    "00/06/12/18Z run supplies the valid-time forcing (hourly to 48 h); "
                    "the Datamart keeps only recent runs (no by-date archive)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.0225,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-152.0, min_lat=27.0, max_lon=-40.0, max_lat=71.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="Environment and Climate Change Canada Data Servers End-use Licence v2.1",
                citation="Environment and Climate Change Canada (ECCC/MSC), HRDPS.",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "HRDPS GRIB2 decoding needs the 'forecast' extra: "
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
        selected = [(tok, internal) for tok, canon, internal, _a in _FIELDS
                    if wanted is None or canon in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by HRDPS")

        init, cycle_hh, run = await asyncio.to_thread(_resolve_latest_run)
        # The accumulated radiation fields de-accumulate along time (value[t] -
        # value[t-1]); to make the first requested step's increment correct, fetch
        # one baseline hour before the window start, then drop it after harmonize.
        want_accum = any(a for _t, canon, _i, a in _FIELDS
                         if (wanted is None or canon in wanted) and a)
        first_lead = int((time_range.start - init).total_seconds() // 3600)
        baseline = time_range.start - pd.Timedelta(hours=1) if (want_accum and first_lead >= 1) else None
        window_start = baseline or time_range.start
        valid_times = pd.date_range(window_start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            lead = int((valid - init).total_seconds() // 3600)
            if not (0 <= lead <= _MAX_LEAD):
                warnings.append(
                    f"HRDPS lead +{lead}h (valid {valid:%Y-%m-%dT%H}) outside the latest "
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
                        # Field absent at this lead (e.g. instantaneous PRATE at +0 h):
                        # skip it, like NOAA radiation/precip at f000.
                        continue
                    raise
                ds = open_message(raw, internal, label="HRDPS")
                per_var.append(subset_2d_grid(ds, bbox, lat_name="latitude", lon_name="longitude"))
            if not per_var:
                return None
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No HRDPS data in [{time_range.start}, {time_range.end}] from the latest "
                f"run {init:%Y%m%d %Hz} (Datamart retains only recent runs; request a "
                f"near-real-time window)"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        if baseline is not None:
            # Drop the extra baseline hour used only to seed the de-accumulation.
            canonical = canonical.sel(time=slice(time_range.start, time_range.end))
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"ECCC HRDPS 2.5 km via MSC Datamart (cfgrib); run {init:%Y%m%d %Hz}; "
                f"canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )
