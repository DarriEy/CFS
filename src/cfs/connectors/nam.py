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
# APCP is de-accumulated to a flux in the connector, then mapped by identity.
_PRECIP_INTERNAL = "APCP"
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(var, canon) for var, _l, canon in _INST_FIELDS
] + [VariableMapping(_PRECIP_INTERNAL, CanonicalVar.PRECIPITATION_FLUX)]

_MAX_LEAD = 84
_HOURLY_MAX = 36  # hourly to f36, 3-hourly after


def _lead_available(lead: int) -> bool:
    if not (1 <= lead <= _MAX_LEAD):
        return False
    return lead <= _HOURLY_MAX or lead % 3 == 0


def _lead_step(lead: int) -> int:
    """The forecast lead spacing at ``lead`` (1 h to f36, 3 h after)."""
    return 1 if lead <= _HOURLY_MAX else 3


def _accum_ref(lead: int) -> int:
    """Start hour of the APCP accumulation window containing ``lead`` (12 h resets)."""
    return 12 * ((lead - 1) // 12)


def _file_url(cycle: datetime, lead: int) -> str:
    return f"{GRIB_BASE}/nam.{cycle:%Y%m%d}/nam.t{cycle:%H}z.awphys{lead:02d}.tm00.grib2"


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
        return [
            ForcingProduct(
                id=f"{self.slug}:awphys_fcst",
                provider=self.slug,
                name="NAM 12 km surface forecast (awphys, North America)",
                description=(
                    "NOAA North American Mesoscale surface forcing, byte-range read "
                    "from the GRIB2 awphys files on the noaa-nam-pds S3 archive. The "
                    "most recent 00/06/12/18Z cycle at/before the requested start "
                    "supplies the valid-time forcing (hourly to f36, 3-hourly to "
                    "f84). Instantaneous fields plus 12-hourly-reset-aware "
                    "de-accumulated precipitation_flux."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.11,  # ~12 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-152.0, min_lat=12.0, max_lon=-49.0, max_lat=61.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP North American Mesoscale (NAM).",
            )
        ]

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

        def _precip_flux(url, records, lead):
            """De-accumulate the run-total APCP at ``lead`` to a per-step flux cube."""
            ref, step = _accum_ref(lead), _lead_step(lead)
            cur_rng = _find(records, "APCP", "surface", f"{ref}-{lead} hour acc fcst")
            if cur_rng is None:
                return None
            cur = read_message_2d(url, cur_rng[0], cur_rng[1], _PRECIP_INTERNAL, bbox, label="NAM")
            prev_lead = lead - step
            if prev_lead != ref:
                # Same 12 h block: subtract the previous lead's run-total (ref-prev_lead).
                purl = _file_url(cycle, prev_lead)
                precs = parse_idx_records(http_range(purl + ".idx", 0, "").decode())
                prng = _find(precs, "APCP", "surface", f"{ref}-{prev_lead} hour acc fcst")
                if prng is None:
                    return None  # cannot de-accumulate without the previous run-total
                prev = read_message_2d(purl, prng[0], prng[1], _PRECIP_INTERNAL, bbox, label="NAM")
                cur = cur - prev
            return (cur / (step * 3600.0)).clip(min=0)

        def _piece(valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not _lead_available(lead):
                warnings.append(
                    f"NAM lead f{lead:02d} (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz} (f01–f36 hourly, then 3-hourly to f84)"
                )
                return None
            url = _file_url(cycle, lead)
            records = parse_idx_records(http_range(url + ".idx", 0, "").decode())
            per_var = []
            for var, level, _c in selected_inst:
                rng = _find(records, var, level)
                if rng is not None:
                    per_var.append(read_message_2d(url, rng[0], rng[1], var, bbox, label="NAM"))
            if want_precip:
                flux = _precip_flux(url, records, lead)
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

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"NAM awphys via noaa-nam-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; APCP de-accumulated (12 h reset-aware); canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )
