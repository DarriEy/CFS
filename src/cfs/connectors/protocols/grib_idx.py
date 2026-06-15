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
   one JSON object per message, not this colon-delimited ``.idx``). Only the
   byte-range fetch and the cfgrib decode are reusable there; its index parser
   belongs in a sibling helper.
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
    and ``end=""`` for the final message in the file.
    """
    for i, (v, lv, sb) in enumerate(idx):
        if v == grib_var and lv == grib_level:
            return sb, (idx[i + 1][2] - 1 if i + 1 < len(idx) else "")
    return None


def open_message(raw: bytes, internal: str, *, label: str = "GRIB") -> xr.Dataset:
    """Decode one GRIB message (bytes) with cfgrib → a single-variable Dataset.

    The decoded variable is renamed to ``internal`` and all GRIB scalar coords
    (step/valid_time/heightAboveGround/surface/number…) are dropped, keeping
    only ``latitude``/``longitude``. ``label`` only flavours the error message.
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
    ds = ds[[data_vars[0]]].rename({data_vars[0]: internal})
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
) -> xr.Dataset | None:
    """Byte-range fetch + decode + bbox-subset one ``(var, level)`` field, or ``None``.

    Returns ``None`` when the message is absent from the ``.idx`` (the caller
    treats this as "not available at this lead", e.g. f000 radiation/precip).
    """
    rng = byte_range(idx, grib_var, grib_level)
    if rng is None:
        return None
    ds = open_message(http_range(url, rng[0], rng[1]), internal, label=label)
    plan = plan_bbox_subset(ds, bbox, lat_name=_LAT, lon_name=_LON)
    # apply_bbox_subset is typed Any (xarray's stubs are incomplete); the subset of
    # a single-variable Dataset is still a Dataset.
    return cast("xr.Dataset", apply_bbox_subset(ds, plan, lat_name=_LAT, lon_name=_LON))


def debucket(cur: xr.Dataset, prev: xr.Dataset, kind: str) -> xr.Dataset:
    """Recover a per-interval value from a running bucket given its first half.

    For an *accumulation* (``kind="acc"``) the interval value is ``cur - prev``;
    for an *average* (``kind="ave"``) the second-half mean is ``2*cur - prev``
    (the full-bucket mean is the mean of its two equal halves). The caller is
    responsible for any non-negativity clip and the per-second flux conversion.
    """
    return cur - prev if kind == "acc" else 2 * cur - prev
