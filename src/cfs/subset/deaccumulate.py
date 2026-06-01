# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Reset-aware de-accumulation of running-total forcing fields.

Several reanalyses store precipitation and radiation as a *running accumulation*
that resets periodically — ERA5 / ERA5-Land accumulate from 00 UTC and reset
each day. Consumers want a per-step amount (then a rate), not the raw running
total, so this must be undone before unit conversion. This is a model-agnostic
transform, so it belongs in CFS's harmonization, not downstream.

The increment at step *t* is ``value[t] - value[t-1]``, except where that diff is
negative — a reset — in which case ``value[t]`` is itself the increment (the
field was just zeroed). The first step uses its own value. Output is clipped at
zero to remove floating-point noise. Stays lazy on dask-backed arrays.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def deaccumulate(da: Any, time_name: str = "time") -> Any:
    """Return per-step increments of a reset-aware running accumulation."""
    import xarray as xr

    if time_name not in da.dims or da.sizes.get(time_name, 0) < 2:
        return da

    prev = da.shift({time_name: 1})
    inc = da - prev
    # First step has no predecessor → use the raw value.
    inc = xr.where(prev.isnull(), da, inc)
    # A negative diff marks an accumulation reset → the raw value is the step.
    inc = xr.where(inc < 0, da, inc)
    inc = inc.clip(min=0)
    inc.attrs = dict(da.attrs)
    inc.attrs["cfs_deaccumulated"] = "true"
    return inc
