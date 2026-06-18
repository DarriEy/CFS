# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""DWD ICON-EU connector — Deutsches Wetterdienst regional forecast (GRIB2).

The ~6.5 km ICON-EU (European nest) forecast from DWD, read anonymously over
plain HTTP from the DWD open-data server. The ICON *global* model is published
only on its native unstructured **icosahedral** grid (which needs a separate
grid-definition file to georeference), whereas ICON-EU is published on a
**regular lat/lon** grid that CFS can subset directly — so this connector ships
ICON-EU; an ICON-global connector (icosahedral remap) is a follow-up.

**Access model** mirrors the ECCC Datamart connectors: the open-data server keeps
only the latest runs (no by-date archive), each variable lives in its own
subdirectory, and every GRIB2 message is a separate **bzip2-compressed** file.
The connector discovers the newest run (its init datetime is embedded in the
filenames, ``…_2026061800_002_T_2M.grib2.bz2``), and for each requested valid
hour downloads the per-variable ``.grib2.bz2``, ``bz2``-decompresses it, and
decodes with cfgrib.

**Variables (6 of 8).** Live-verified 2026-06-18 GRIB metadata:
``T_2M``/``PS``/``U_10M``/``V_10M`` are instantaneous canonical SI (identity);
``TOT_PREC`` is an accumulation (kg m⁻², ``stepType=accum``) → de-accumulate to a
per-step total and divide by the step seconds → flux; surface downwelling
shortwave is the sum of direct ``ASWDIR_S`` + diffuse ``ASWDIFD_S`` (both
**time-averaged-since-run-start** W m⁻², ``stepType=avg``) → de-averaged with the
forecast-lead weights to a per-step mean flux. ICON-EU regular-lat-lon does **not**
publish a downwelling-longwave field (only *net* ``ATHB_S``) nor specific humidity
(only relative ``RELHUM_2M``), so those two canonical variables are not served.

Restricted to the hourly forecast range (lead ≤ 78 h) so the de-accumulation /
de-averaging step is always a clean 1-hour interval; the 3-hourly tail
(78–120 h) is a follow-up. Needs the ``cfgrib``/``eccodes`` stack (the
``forecast`` extra). Anonymous (CC-BY-4.0), so live-verifiable.
"""

from __future__ import annotations

import asyncio
import bz2
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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

ICON_BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
_MAX_LEAD = 78  # hourly range; the 3-hourly tail (78-120 h) is a follow-up
_CYCLES = (18, 12, 6, 0)  # newest-first preference order

# Instantaneous surface fields: DWD subdir, filename VAR token, canonical, internal.
# All canonical SI → identity.
_INST: list[tuple[str, str, CanonicalVar, str]] = [
    ("t_2m", "T_2M", CanonicalVar.AIR_TEMPERATURE, "_t2m"),
    ("ps", "PS", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp"),
    ("u_10m", "U_10M", CanonicalVar.EASTWARD_WIND, "_u10"),
    ("v_10m", "V_10M", CanonicalVar.NORTHWARD_WIND, "_v10"),
]
# Precipitation: TOT_PREC accumulated since run start (kg m-2) → de-accumulate +
# /3600 s → flux. Surface shortwave = ASWDIR_S + ASWDIFD_S, running averages
# (W m-2) → de-averaged in the connector to a per-step mean flux.
_PRECIP = ("tot_prec", "TOT_PREC", "_tp")
_SW_DIRECT = ("aswdir_s", "ASWDIR_S")
_SW_DIFFUSE = ("aswdifd_s", "ASWDIFD_S")

# Canonical vars this connector can deliver (6 of 8).
_OFFERED: frozenset[CanonicalVar] = frozenset(
    {c for _d, _v, c, _i in _INST}
    | {CanonicalVar.PRECIPITATION_FLUX, CanonicalVar.SHORTWAVE_RADIATION_DOWN}
)

_MAPPINGS: list[VariableMapping] = [
    *[VariableMapping(internal, canon) for _d, _v, canon, internal in _INST],
    VariableMapping(_PRECIP[2], CanonicalVar.PRECIPITATION_FLUX, scale=1.0 / 3600.0,
                    deaccumulate=True, note="ICON TOT_PREC accum (kg m-2) -> per-hour total / 3600 s -> flux"),
    VariableMapping("_swdown", CanonicalVar.SHORTWAVE_RADIATION_DOWN,
                    note="ICON ASWDIR_S+ASWDIFD_S running average -> per-step mean flux (W m-2)"),
]


def _file_url(cycle_hh: int, lead: int, run_stamp: str, subdir: str, var: str) -> str:
    hh = f"{cycle_hh:02d}"
    name = f"icon-eu_europe_regular-lat-lon_single-level_{run_stamp}_{lead:03d}_{var}.grib2.bz2"
    return f"{ICON_BASE}/{hh}/{subdir}/{name}"


def _listdir(url: str) -> list[str]:
    """Return the href entries of an Apache directory index (anonymous)."""
    with urllib.request.urlopen(url, timeout=get_settings().provider_timeout_s) as r:  # noqa: S310 - https
        html = r.read().decode("utf-8", "ignore")
    return [h for h in re.findall(r'href="([^"]+)"', html) if not h.startswith("?") and h not in ("../", "/")]


def _resolve_latest_run() -> tuple[datetime, int, str]:
    """Discover the newest available ICON-EU run → (init_dt, cycle_hh, YYYYMMDDHH).

    The open-data server keeps only recent runs; the init stamp lives in the
    filenames (``…_2026061800_…``). Picks the newest present cycle and reads its
    stamp from a ``t_2m`` file.
    """
    cycles_present = {
        int(h.strip("/"))
        for h in _listdir(f"{ICON_BASE}/")
        if h.strip("/").isdigit()
    }
    for hh in _CYCLES:
        if hh not in cycles_present:
            continue
        for f in _listdir(f"{ICON_BASE}/{hh:02d}/t_2m/"):
            m = re.search(r"_(\d{10})_\d{3}_T_2M\.grib2\.bz2", f)
            if m and int(m.group(1)[8:10]) == hh:
                return datetime.strptime(m.group(1), "%Y%m%d%H"), hh, m.group(1)
    raise ConnectorError("dwd_icon", "No ICON-EU run found on the DWD open-data server")


def _open_bz2_grib(url: str, internal: str, label: str):
    """Download a ``.grib2.bz2``, decompress, decode one message → renamed Dataset."""
    raw = bz2.decompress(http_range(url, 0, ""))
    return open_message(raw, internal, label=label)


@register("dwd_icon")
class DWDICONConnector(BaseForcingConnector):
    slug = "dwd_icon"
    display_name = "DWD ICON-EU forecast (~6.5 km, Europe)"
    base_url = ICON_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:eu_regular",
                provider=self.slug,
                name="ICON-EU regular-lat-lon surface forecast",
                description=(
                    "DWD ICON-EU (~6.5 km European nest) surface forcing on the regular "
                    "lat/lon grid, downloaded per-variable (bzip2 GRIB2) from the DWD "
                    "open-data server and decoded with cfgrib. The newest 00/06/12/18Z "
                    "run supplies the valid-time forcing (hourly to 78 h). Serves 6 of 8 "
                    "canonical variables (no downwelling longwave / specific humidity on "
                    "the EU regular grid)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.0625,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-23.5, min_lat=29.5, max_lon=62.5, max_lat=70.5),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="CC-BY-4.0 (DWD open data / GeoNutzV)",
                citation="Deutscher Wetterdienst (DWD), ICON-EU.",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "ICON GRIB2 decoding needs the 'forecast' extra: "
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
        inst = [(d, v, i) for d, v, _c, i in _INST if wanted is None or _c in wanted]
        want_precip = wanted is None or CanonicalVar.PRECIPITATION_FLUX in wanted
        want_sw = wanted is None or CanonicalVar.SHORTWAVE_RADIATION_DOWN in wanted
        if not inst and not want_precip and not want_sw:
            raise SubsetError(
                "None of the requested variables are offered by ICON-EU "
                f"(this product serves {sorted(c.value for c in _OFFERED)})"
            )

        init, cycle_hh, stamp = await asyncio.to_thread(_resolve_latest_run)
        # Precip de-accumulates and shortwave de-averages along time, both of which
        # need the previous lead; fetch one baseline hour before the window start
        # so the first requested step is correct, then drop it after harmonize.
        need_prev = want_precip or want_sw
        first_lead = int((time_range.start - init).total_seconds() // 3600)
        baseline = time_range.start - pd.Timedelta(hours=1) if (need_prev and first_lead >= 1) else None
        window_start = baseline or time_range.start
        valid_times = pd.date_range(window_start, time_range.end, freq="h")
        warnings: list[str] = []

        def _fetch(subdir, var, internal, valid_lead):
            url = _file_url(cycle_hh, valid_lead, stamp, subdir, var)
            try:
                ds = _open_bz2_grib(url, internal, "ICON-EU")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                raise
            return apply_bbox_subset(
                ds, plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude"),
                lat_name="latitude", lon_name="longitude",
            )

        def _piece(valid):
            lead = int((valid - init).total_seconds() // 3600)
            if not (0 <= lead <= _MAX_LEAD):
                warnings.append(
                    f"ICON-EU lead +{lead}h (valid {valid:%Y-%m-%dT%H}) outside the latest "
                    f"run {init:%Y%m%d %Hz} (hourly forecast covers +0..+{_MAX_LEAD}h)"
                )
                return None
            parts = []
            for subdir, var, internal in inst:
                p = _fetch(subdir, var, internal, lead)
                if p is not None:
                    parts.append(p)
            if want_precip:
                p = _fetch(_PRECIP[0], _PRECIP[1], _PRECIP[2], lead)
                if p is not None:
                    parts.append(p)
            if want_sw:
                direct = _fetch(_SW_DIRECT[0], _SW_DIRECT[1], "_swdown", lead)
                diffuse = _fetch(_SW_DIFFUSE[0], _SW_DIFFUSE[1], "_swdif", lead)
                if direct is not None and diffuse is not None:
                    # Surface downwelling shortwave = direct + diffuse (both running avgs).
                    parts.append((direct["_swdown"] + diffuse["_swdif"]).rename("_swdown").to_dataset())
            if not parts:
                return None
            merged = xr.merge(parts, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No ICON-EU data in [{time_range.start}, {time_range.end}] from the latest "
                f"run {init:%Y%m%d %Hz} (server retains only recent runs; request a "
                f"near-real-time window)"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        # De-average the running-average shortwave to a per-step mean flux using the
        # forecast-lead weights: inst(t) = lead(t)*A(t) - lead(t-1)*A(t-1) (1-h step).
        if want_sw and "_swdown" in ds_all:
            import numpy as np

            lead_vals = (ds_all["time"].values - np.datetime64(init)) / np.timedelta64(1, "h")
            lead_h = xr.DataArray(lead_vals, coords={"time": ds_all["time"]}, dims="time")
            a = ds_all["_swdown"]
            sw = lead_h * a - (lead_h - 1) * a.shift(time=1)
            # First time has no predecessor: at lead 0 the flux is 0; else seeded by baseline.
            sw = sw.where(a.shift(time=1).notnull(), a if baseline is None else sw)
            ds_all["_swdown"] = sw.clip(min=0)

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
                f"DWD ICON-EU regular-lat-lon via opendata.dwd.de (bz2+cfgrib); "
                f"run {init:%Y%m%d %Hz}; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
