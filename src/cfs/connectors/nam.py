# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NAM connector — NOAA North American Mesoscale forecast (AWS GRIB2).

The 12 km North-America forecast in the Tier-B short-range set, read from the
public ``noaa-nam-pds`` S3 archive (anonymous) with the shared Herbie ``.idx``
byte-range + cfgrib machinery (:mod:`cfs.connectors.protocols.grib_idx`).

Reads the ``awphys`` product (Lambert Conformal grid 218, ~12 km, North
America). Forecast model like ``gfs``/``hrrr``/``rap``: the most recent
00/06/12/18Z cycle at or before the start supplies each valid hour
(``lead = valid − cycle``). Leads are **hourly to f36, then 3-hourly to f84**
(live-probed 2026-06-13).

The instantaneous fields are SI identity — ``TMP``/``SPFH``/``DPT`` 2 m,
``PRES`` surface, ``UGRD``/``VGRD`` 10 m, and ``DSWRF``/``DLWRF`` surface
(instantaneous, "N hour fcst", *not* averaged). ``SPFH`` 2 m is shipped
directly (no humidity derivation).

**Precipitation** is the awkward field — NAM ships no instantaneous ``PRATE``,
only accumulated ``APCP``, and its accumulation reference *resets every 12 h*:
f01–f12 are ``0-N``, then f13 = ``12-13``, f24 = ``12-24``, f36 = ``24-36`` …
(reference ``= 12·⌊(N−1)/12⌋``), with extra 3-hour-bucket ``APCP`` messages at
some leads. So this connector picks the **run-total** ``APCP`` message by its
forecast-window string (``"{ref}-{N} hour acc fcst"``) and de-accumulates to
the per-step increment: since 12 is divisible by both lead steps (1 and 3) a
step never straddles a reset, giving the clean rule

  * ``inc(N) = runtotal(N)``               when ``N − step == ref(N)`` (first
    lead after a reset — the run-total already spans exactly one step), else
  * ``inc(N) = runtotal(N) − runtotal(N − step)``  (both in the same 12 h block),

then ``flux = inc / (step·3600)`` (kg m⁻² s⁻¹). The subtraction case fetches
the previous lead's run-total (an extra ``.idx`` + message), like ``gefs``'s
de-bucketing.

Only the 12 km ``awphys`` product is offered; the 3 km CONUS nest
(``conusnest``) is a follow-up. Needs the ``forecast`` extra. Anonymous, so
live-verifiable.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import (
    COMBINED_WIND_CFNAME,
    cycle_for,
    http_range,
    parse_idx_records,
    read_message_2d,
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

GRIB_BASE = "https://noaa-nam-pds.s3.amazonaws.com"

# Instantaneous surface fields in the awphys .idx, (grib_var, grib_level,
# canonical). All instantaneous SI → identity. Precipitation is handled
# separately (APCP de-accumulation), not in this table.
_INST_FIELDS: list[tuple[str, str, CanonicalVar]] = [
    ("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE),
    ("SPFH", "2 m above ground", CanonicalVar.SPECIFIC_HUMIDITY),
    ("DPT", "2 m above ground", CanonicalVar.DEWPOINT_TEMPERATURE),
    ("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE),
    ("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND),
    ("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND),
    ("DSWRF", "surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    ("DLWRF", "surface", CanonicalVar.LONGWAVE_RADIATION_DOWN),
]
_INST_MAPPINGS = [VariableMapping(var, canon) for var, _l, canon in _INST_FIELDS]
# awphys de-accumulates APCP to a flux; conusnest ships an instantaneous PRATE
# flux directly. Both map to precipitation_flux by identity.
_MAPPINGS_APCP: list[VariableMapping] = [
    *_INST_MAPPINGS, VariableMapping("APCP", CanonicalVar.PRECIPITATION_FLUX)
]
_MAPPINGS_PRATE: list[VariableMapping] = [
    *_INST_MAPPINGS, VariableMapping("PRATE", CanonicalVar.PRECIPITATION_FLUX)
]

# Two products. awphys: 12 km North America, hourly to f36 then 3-hourly to f84,
# precip via 12 h-reset-aware APCP de-accumulation (no PRATE). conusnest: 3 km
# CONUS nest, hourly to f60, all-instantaneous identity SI — including an
# instantaneous PRATE flux and instantaneous DSWRF/DLWRF (the nest also publishes
# ave/max radiation + max precip, so the instantaneous message is selected by its
# "{lead} hour fcst" window string). Live-probed 2026-06-15.
_PRODUCTS: dict[str, dict] = {
    "awphys_fcst": {
        "name": "NAM 12 km surface forecast (awphys, North America)",
        "grid": "awphys", "domain": "North America", "res": 0.11,
        "bbox": (-152.0, 12.0, -49.0, 61.0),
        "max_lead": 84, "hourly_max": 36, "precip": "apcp",
    },
    "conusnest_fcst": {
        "name": "NAM 3 km surface forecast (CONUS nest)",
        "grid": "conusnest", "domain": "CONUS", "res": 0.027,
        "bbox": (-134.0, 21.0, -61.0, 53.0),
        "max_lead": 60, "hourly_max": 60, "precip": "prate",
    },
}


def _lead_available(lead: int, max_lead: int, hourly_max: int) -> bool:
    if not (1 <= lead <= max_lead):
        return False
    return lead <= hourly_max or lead % 3 == 0


def _lead_step(lead: int, hourly_max: int) -> int:
    """The forecast lead spacing at ``lead`` (1 h to ``hourly_max``, 3 h after)."""
    return 1 if lead <= hourly_max else 3


def _accum_ref(lead: int) -> int:
    """Start hour of the APCP accumulation window containing ``lead`` (12 h resets)."""
    return 12 * ((lead - 1) // 12)


def _file_url(grid: str, cycle: datetime, lead: int) -> str:
    d, h = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    if grid == "conusnest":
        return f"{GRIB_BASE}/nam.{d}/nam.t{h}z.conusnest.hiresf{lead:02d}.tm00.grib2"
    return f"{GRIB_BASE}/nam.{d}/nam.t{h}z.awphys{lead:02d}.tm00.grib2"


def _find(records, grib_var: str, grib_level: str, fcst: str | None = None):
    """First ``(start, end)`` whose (var, level[, fcst]) match, or ``None``."""
    for v, lv, sb, end, fc in records:
        if v == grib_var and lv == grib_level and (fcst is None or fc == fcst):
            return sb, end
    return None


@register("nam")
class NAMConnector(BaseForcingConnector):
    slug = "nam"
    display_name = "NOAA NAM atmospheric forecast (12 km, North America)"
    base_url = GRIB_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        products = []
        for suffix, spec in _PRODUCTS.items():
            mappings = _MAPPINGS_APCP if spec["precip"] == "apcp" else _MAPPINGS_PRATE
            lo, la, hi, ha = spec["bbox"]
            if spec["precip"] == "apcp":
                precip_desc = "12-hourly-reset-aware de-accumulated precipitation_flux"
                lead_desc = "hourly to f36, 3-hourly to f84"
            else:
                precip_desc = "instantaneous precipitation_flux (PRATE)"
                lead_desc = "hourly to f60"
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{suffix}",
                    provider=self.slug,
                    name=spec["name"],
                    description=(
                        f"NOAA North American Mesoscale surface forcing ({spec['domain']}), "
                        "byte-range read from the GRIB2 files on the noaa-nam-pds S3 "
                        "archive. The most recent 00/06/12/18Z cycle at/before the "
                        f"requested start supplies the valid-time forcing ({lead_desc}). "
                        f"Instantaneous fields plus {precip_desc}."
                    ),
                    variables=[
                        ProductVariable(canonical=m.canonical, source_name=m.source_name)
                        for m in mappings
                    ],
                    resolution_deg=spec["res"],
                    crs="EPSG:4326",
                    bbox=BoundingBox(min_lon=lo, min_lat=la, max_lon=hi, max_lat=ha),
                    temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                    protocol=Protocol.REST,
                    license="U.S. Government work / NOAA Open Data (public domain)",
                    citation="NOAA NCEP North American Mesoscale (NAM).",
                )
            )
        return products

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "NAM GRIB2 decoding needs the 'forecast' extra: "
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
        grid, max_lead, hourly_max = spec["grid"], spec["max_lead"], spec["hourly_max"]
        mappings = _MAPPINGS_APCP if spec["precip"] == "apcp" else _MAPPINGS_PRATE
        settings = get_settings()
        self._guard_area(bbox, settings)
        self._require_cfgrib()

        wanted = set(variables) if variables else None
        selected_inst = [f for f in _INST_FIELDS if wanted is None or f[2] in wanted]
        want_precip = wanted is None or CanonicalVar.PRECIPITATION_FLUX in wanted
        if not selected_inst and not want_precip:
            raise SubsetError("None of the requested variables are offered by NAM")

        cycle = cycle_for(time_range.start, step_h=6)
        valid_times = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _apcp_flux(url, records, lead):
            """De-accumulate the run-total APCP at ``lead`` to a per-step flux cube."""
            ref, step = _accum_ref(lead), _lead_step(lead, hourly_max)
            cur_rng = _find(records, "APCP", "surface", f"{ref}-{lead} hour acc fcst")
            if cur_rng is None:
                return None
            cur = read_message_2d(url, cur_rng[0], cur_rng[1], "APCP", bbox, label="NAM")
            prev_lead = lead - step
            if prev_lead != ref:
                # Same 12 h block: subtract the previous lead's run-total (ref-prev_lead).
                purl = _file_url(grid, cycle, prev_lead)
                precs = parse_idx_records(http_range(purl + ".idx", 0, "").decode())
                prng = _find(precs, "APCP", "surface", f"{ref}-{prev_lead} hour acc fcst")
                if prng is None:
                    return None  # cannot de-accumulate without the previous run-total
                prev = read_message_2d(purl, prng[0], prng[1], "APCP", bbox, label="NAM")
                cur = cur - prev
            return (cur / (step * 3600.0)).clip(min=0)

        def _piece(valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not _lead_available(lead, max_lead, hourly_max):
                warnings.append(
                    f"NAM {grid} lead f{lead:02d} (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz}"
                )
                return None
            url = _file_url(grid, cycle, lead)
            records = parse_idx_records(http_range(url + ".idx", 0, "").decode())
            # The nest publishes ave/max variants too, so pin the instantaneous
            # message by its "{lead} hour fcst" window; awphys has only the one.
            inst_fcst = f"{lead} hour fcst" if grid == "conusnest" else None
            per_var = []
            for var, level, _c in selected_inst:
                rng = _find(records, var, level, inst_fcst)
                if rng is not None:
                    # NAM packs 10 m U+V in one GRIB message (shared .idx offset);
                    # prefer pulls the right component from that combined message.
                    per_var.append(read_message_2d(url, rng[0], rng[1], var, bbox, label="NAM",
                                                   prefer=COMBINED_WIND_CFNAME.get(var)))
            if want_precip:
                if spec["precip"] == "apcp":
                    flux = _apcp_flux(url, records, lead)
                else:  # conusnest: instantaneous PRATE flux (identity)
                    prng = _find(records, "PRATE", "surface", f"{lead} hour fcst")
                    flux = (read_message_2d(url, prng[0], prng[1], "PRATE", bbox, label="NAM")
                            if prng is not None else None)
                if flux is not None:
                    per_var.append(flux)
            if not per_var:
                return None
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No NAM data in [{time_range.start}, {time_range.end}] from cycle "
                f"{cycle:%Y%m%d %Hz} (archive retains a long history; very old cycles age off)"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        precip_note = ("APCP de-accumulated (12 h reset-aware)" if spec["precip"] == "apcp"
                       else "instantaneous PRATE")
        canonical = harmonize(ds_all, mappings, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"NAM {grid} via noaa-nam-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; {precip_note}; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )
