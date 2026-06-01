# SPDX-License-Identifier: MIT
"""Reset-aware de-accumulation tests (the ERA5-Land daily-reset gotcha)."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.subset.deaccumulate import deaccumulate


def _series(values):
    return xr.DataArray(
        np.array(values, dtype="float64").reshape(-1, 1, 1),
        dims=("time", "latitude", "longitude"),
        coords={"time": np.arange(len(values)), "latitude": [0.0], "longitude": [0.0]},
    )


def test_monotonic_accumulation_becomes_increments():
    # Accumulates 0,1,3,6 -> increments 0,1,2,3 (first step = raw value).
    da = _series([0.0, 1.0, 3.0, 6.0])
    out = deaccumulate(da).values.ravel()
    assert list(out) == [0.0, 1.0, 2.0, 3.0]


def test_daily_reset_uses_raw_value_after_reset():
    # Day 1 accumulates 1,3,6 then resets: 2,5 -> increments 1,2,3, then 2,3.
    da = _series([1.0, 3.0, 6.0, 2.0, 5.0])
    out = deaccumulate(da).values.ravel()
    assert list(out) == [1.0, 2.0, 3.0, 2.0, 3.0]


def test_output_is_nonnegative():
    da = _series([5.0, 4.0, 10.0])  # 4<5 is a reset -> raw 4; then 10-4=6
    out = deaccumulate(da).values.ravel()
    assert (out >= 0).all()
    assert list(out) == [5.0, 4.0, 6.0]


def test_single_step_passthrough():
    da = _series([7.0])
    out = deaccumulate(da).values.ravel()
    assert list(out) == [7.0]


def test_marks_attr():
    da = _series([0.0, 1.0, 2.0])
    assert deaccumulate(da).attrs.get("cfs_deaccumulated") == "true"
