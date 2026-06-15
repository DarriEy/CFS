# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Shared GRIB2 ``.idx`` byte-range helpers for NOAA forecast connectors.

NOAA forecast archives (GFS, GEFS, and the planned HRRR/RAP/NAM/NBM forecast
streams) publish GRIB2 files alongside a ``.idx`` sidecar — a byte-offset index
of every GRIB message. Rather than download a multi-hundred-MB file to read a
handful of surface fields, a connector reads the ``.idx``, computes the byte
range of each message it wants, HTTP **byte-range** fetches just those bytes,
and decodes each with cfgrib (the "Herbie" pattern). A basin-scale surface
fetch then transfers a few MB per lead hour instead of gigabytes.

This module factors out the parts that are identical across those connectors —
the ``.idx`` parse, the range fetch, the single-message cfgrib decode, the
``(var, level) → byte range`` lookup, the cycle-flooring helper, and the
bucket de-accumulation arithmetic. What stays connector-local is everything
product-specific: the file-URL template, the available-lead table, and the
canonical variable mapping.

cfgrib (the ``forecast`` extra) is imported lazily inside :func:`open_message`
so importing this module never requires it.

.. note::
   ECMWF open-data uses a *different* index format (a ``.index`` sidecar with
   one JSON object per message, not this colon-delimited ``.idx``), handled by
   :func:`parse_ecmwf_index`; the byte-range fetch (:func:`http_range`), decode
   (:func:`open_message`), and regular-grid window (:func:`read_message`) are
   shared.
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, cast

from cfs.core.config import get_settings
from cfs.core.exceptions import SubsetError
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.grid2d import subset_2d_grid

if TYPE_CHECKING:
    import xarray as xr

    from cfs.core.models import BoundingBox

# GRIB messages carry their grid as 2-D latitude/longitude coords on decode.
_LAT = "latitude"
_LON = "longitude"


def cycle_for(start: datetime, step_h: int = 6) -> datetime:
    """Most recent model cycle at or before ``start``.

    ``step_h`` is the cycle spacing in hours: 6 for GFS/GEFS (00/06/12/18 UTC),
    1 for the hourly-cycling models (HRRR/RAP).
    """
    return start.replace(hour=(start.hour // step_h) * step_h, minute=0, second=0, microsecond=0)


def parse_idx(text: str) -> list[tuple[str, str, int]]:
    """Parse a NOAA GRIB2 ``.idx`` into ``[(grib_var, grib_level, start_byte)]``.

    Lines look like ``msgnum:start_byte:date:VAR:level:forecast:`` — in file
    order, so a message's end byte is the next record's start minus one.
    """
    out: list[tuple[str, str, int]] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split(":")
        if len(parts) >= 5:
            out.append((parts[3], parts[4], int(parts[1])))
    return out


def http_range(url: str, start: int, end: int | str) -> bytes:
    """HTTP byte-range GET ``[start, end]`` (``end=""`` means to end-of-file)."""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})  # noqa: S310 - https S3
    with urllib.request.urlopen(req, timeout=get_settings().provider_timeout_s) as r:
        data: bytes = r.read()
    return data


def byte_range(
    idx: list[tuple[str, str, int]], grib_var: str, grib_level: str
) -> tuple[int, int | str] | None:
    """Locate the ``(start, end)`` byte range of one ``(var, level)`` message.

    Returns ``None`` if the message is absent (e.g. radiation/precip at f000),
    and ``end=""`` for the final message in the file. The end is the next message
    with a **strictly greater** start byte: some products co-locate fields in one
    GRIB message (e.g. RAP/NAM pack 10 m U and V wind together, so both ``.idx``
    entries share an offset), and the naive next-row end would be ``start − 1``.
    """
    for i, (v, lv, sb) in enumerate(idx):
        if v == grib_var and lv == grib_level:
            return sb, _end_after(sb, [r[2] for r in idx[i + 1:]])
    return None


def _end_after(start: int, later_starts: list[int]) -> int | str:
    """End byte = (first later start strictly greater than ``start``) − 1, or ``""``."""
    for nsb in later_starts:
        if nsb > start:
            return nsb - 1
    return ""


# NOAA products that pack >1 field in a single GRIB message (RAP/NAM put 10 m
# U and V wind in one message). Maps the .idx GRIB var name to the cfgrib
# variable name to extract from the multi-variable decode (see ``prefer`` below).
COMBINED_WIND_CFNAME = {"UGRD": "u10", "VGRD": "v10"}


def open_message(
    raw: bytes, internal: str, *, label: str = "GRIB", prefer: str | None = None
) -> xr.Dataset:
    """Decode one GRIB message (bytes) with cfgrib → a single-variable Dataset.

    The decoded variable is renamed to ``internal`` and all GRIB scalar coords
    (step/valid_time/heightAboveGround/surface/number…) are dropped, keeping
    only ``latitude``/``longitude``. ``label`` only flavours the error message.

    A few NOAA products pack more than one field in a single GRIB message (e.g.
    RAP/NAM 10 m U+V wind). When the decode yields several variables, ``prefer``
    (a cfgrib variable name such as ``"u10"``) selects the requested component;
    otherwise the first variable is used.
    """
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
        raise SubsetError(f"{label} message for {internal} decoded no variable")
    if prefer is not None and prefer not in data_vars:
        # Fail loud rather than silently take data_vars[0] — for a combined U/V
        # message that fallback would mislabel one wind component as the other.
        raise SubsetError(
            f"{label}: requested component {prefer!r} not in decoded variables "
            f"{data_vars} (for {internal})"
        )
    name = prefer if prefer is not None else data_vars[0]
    ds = ds[[name]].rename({name: internal})
    drop = [c for c in ds.coords if c not in (_LAT, _LON)]
    return ds.drop_vars(drop, errors="ignore")


def read_field(
    url: str,
    idx: list[tuple[str, str, int]],
    grib_var: str,
    grib_level: str,
    internal: str,
    bbox: BoundingBox,
    *,
    label: str = "GRIB",
    prefer: str | None = None,
) -> xr.Dataset | None:
    """Byte-range fetch + decode + bbox-subset one ``(var, level)`` field, or ``None``.

    Returns ``None`` when the message is absent from the ``.idx`` (the caller
    treats this as "not available at this lead", e.g. f000 radiation/precip).
    ``prefer`` selects a component from a multi-variable (combined) message.
    """
    rng = byte_range(idx, grib_var, grib_level)
    if rng is None:
        return None
    return read_message(url, rng[0], rng[1], internal, bbox, label=label, prefer=prefer)


def read_message(
    url: str,
    start: int,
    end: int | str,
    internal: str,
    bbox: BoundingBox,
    *,
    label: str = "GRIB",
    prefer: str | None = None,
) -> xr.Dataset:
    """Byte-range fetch + decode + regular-grid bbox-subset one message at ``[start, end]``.

    The decode/window half of :func:`read_field`, split out so a caller that
    selected a message by something other than ``(var, level)`` — e.g. an ECMWF
    ``.index`` ``_offset``/``_length`` — can reuse it. Regular 1-D lat/lon grid
    (0–360 longitude is normalized by :func:`cfs.subset.bbox.plan_bbox_subset`).
    ``prefer`` selects a component from a multi-variable (combined) message.
    """
    ds = open_message(http_range(url, start, end), internal, label=label, prefer=prefer)
    plan = plan_bbox_subset(ds, bbox, lat_name=_LAT, lon_name=_LON)
    # apply_bbox_subset is typed Any (xarray's stubs are incomplete); the subset of
    # a single-variable Dataset is still a Dataset.
    return cast("xr.Dataset", apply_bbox_subset(ds, plan, lat_name=_LAT, lon_name=_LON))


def read_field_2d(
    url: str,
    idx: list[tuple[str, str, int]],
    grib_var: str,
    grib_level: str,
    internal: str,
    bbox: BoundingBox,
    *,
    label: str = "GRIB",
    buffer: int = 2,
    prefer: str | None = None,
) -> xr.Dataset | None:
    """Like :func:`read_field` but for **projected 2-D grids** (LCC HRRR/RAP/NAM).

    cfgrib attaches 2-D ``latitude``/``longitude`` over the native ``y``/``x``
    dims, so the bbox is applied as an index window on those dims
    (:func:`cfs.subset.grid2d.subset_2d_grid`) rather than by 1-D coordinate
    slicing. Returns ``None`` when the message is absent from the ``.idx``.
    ``prefer`` selects a component from a multi-variable (combined) message.
    """
    rng = byte_range(idx, grib_var, grib_level)
    if rng is None:
        return None
    return read_message_2d(url, rng[0], rng[1], internal, bbox,
                           label=label, buffer=buffer, prefer=prefer)


def read_message_2d(
    url: str,
    start: int,
    end: int | str,
    internal: str,
    bbox: BoundingBox,
    *,
    label: str = "GRIB",
    buffer: int = 2,
    prefer: str | None = None,
) -> xr.Dataset:
    """Byte-range fetch + decode + 2-D bbox-window one message at ``[start, end]``.

    The decode/window half of :func:`read_field_2d`, split out so a caller that
    selected a message by something other than ``(var, level)`` — e.g. a
    forecast-window string (NAM's run-total vs 3-hour APCP) — can reuse it.
    ``prefer`` selects a component from a multi-variable (combined) message.
    """
    ds = open_message(http_range(url, start, end), internal, label=label, prefer=prefer)
    return cast(
        "xr.Dataset",
        subset_2d_grid(ds, bbox, lat_name=_LAT, lon_name=_LON, buffer=buffer),
    )


def parse_idx_records(text: str) -> list[tuple[str, str, int, int | str, str]]:
    """Parse a ``.idx`` into ordered ``(var, level, start, end, fcst)`` records.

    Like :func:`parse_idx` but retains the forecast-window field (``parts[5]``)
    and precomputes each message's end byte, so a caller can disambiguate messages
    that share ``(var, level)`` but differ by accumulation window — e.g. NAM's
    run-total (``0-12 hour acc``) vs its 3-hour bucket (``9-12 hour acc``)
    ``APCP`` messages. The end is the next message with a **strictly greater**
    start byte, so fields co-located in one GRIB message (e.g. NAM 10 m U+V wind,
    which share an offset) get the full message extent rather than ``start − 1``.
    """
    rows: list[tuple[str, str, int, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        p = line.split(":")
        if len(p) >= 6:
            rows.append((p[3], p[4], int(p[1]), p[5]))
    starts = [r[2] for r in rows]
    out: list[tuple[str, str, int, int | str, str]] = []
    for i, (v, lv, sb, fc) in enumerate(rows):
        out.append((v, lv, sb, _end_after(sb, starts[i + 1:]), fc))
    return out


def parse_ecmwf_index(
    text: str,
) -> list[tuple[str, str, str, str | None, int, int]]:
    """Parse an ECMWF ``.index`` into ``(param, levtype, step, number, offset, length)``.

    ECMWF's sidecar is one JSON object per line (not the NOAA colon ``.idx``),
    and gives each message's byte range **directly** via ``_offset``/``_length``
    — so there is no next-start-minus-one arithmetic. ``number`` is the ensemble
    member for the ``enfo`` stream (``"1"``…``"50"``) and ``None`` for the
    deterministic ``oper`` stream; each (param, levtype, number) is unique within
    one single-step index file.
    """
    import json

    out: list[tuple[str, str, str, str | None, int, int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        # Coerce `number` to str (the feed may emit it as a JSON int) so member
        # matching in _find is type-stable, mirroring `step`.
        number = str(r["number"]) if "number" in r else None
        out.append(
            (r.get("param"), r.get("levtype"), str(r.get("step")), number,
             int(r["_offset"]), int(r["_length"])),
        )
    return out


def debucket(cur: xr.Dataset, prev: xr.Dataset, kind: str) -> xr.Dataset:
    """Recover a per-interval value from a running bucket given its first half.

    For an *accumulation* (``kind="acc"``) the interval value is ``cur - prev``;
    for an *average* (``kind="ave"``) the second-half mean is ``2*cur - prev``
    (the full-bucket mean is the mean of its two equal halves). The caller is
    responsible for any non-negativity clip and the per-second flux conversion.
    """
    return cur - prev if kind == "acc" else 2 * cur - prev
