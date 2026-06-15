# SPDX-License-Identifier: MIT
"""Unit tests for the shared GRIB .idx byte-range helpers (cfs.connectors.protocols.grib_idx)."""

from __future__ import annotations

from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")
np = pytest.importorskip("numpy")

from cfs.connectors.protocols.grib_idx import (
    byte_range,
    cycle_for,
    debucket,
    parse_idx,
)


def test_parse_idx_skips_blank_and_short_lines():
    idx = (
        "1:0:d=2026:TMP:surface:anl:\n"
        "\n"                       # blank → skipped
        "bad:line\n"               # < 5 fields → skipped
        "2:100:d=2026:APCP:surface:0-1 hour acc:\n"
    )
    recs = parse_idx(idx)
    assert recs == [("TMP", "surface", 0), ("APCP", "surface", 100)]


def test_byte_range_uses_next_start_minus_one():
    idx = [("TMP", "surface", 0), ("APCP", "surface", 100), ("PRES", "surface", 250)]
    assert byte_range(idx, "TMP", "surface") == (0, 99)
    assert byte_range(idx, "APCP", "surface") == (100, 249)


def test_byte_range_last_message_is_open_ended():
    idx = [("TMP", "surface", 0), ("PRES", "surface", 250)]
    # the final message has no successor → end-of-file marker ""
    assert byte_range(idx, "PRES", "surface") == (250, "")


def test_byte_range_absent_message_is_none():
    idx = [("TMP", "surface", 0)]
    assert byte_range(idx, "DSWRF", "surface") is None  # e.g. radiation at f000


def test_cycle_for_6h_default_matches_gfs_gefs():
    assert cycle_for(datetime(2026, 6, 1, 1, 30)) == datetime(2026, 6, 1, 0)
    assert cycle_for(datetime(2026, 6, 1, 7)) == datetime(2026, 6, 1, 6)
    assert cycle_for(datetime(2026, 6, 1, 23, 59)) == datetime(2026, 6, 1, 18)


def test_cycle_for_hourly_step_for_hrrr_rap():
    # HRRR/RAP cycle every hour; step_h=1 floors to the hour.
    assert cycle_for(datetime(2026, 6, 1, 13, 42), step_h=1) == datetime(2026, 6, 1, 13)
    assert cycle_for(datetime(2026, 6, 1, 0, 5), step_h=1) == datetime(2026, 6, 1, 0)


def _da(value: float) -> xr.Dataset:
    return xr.Dataset({"x": (("y",), np.array([value], dtype="float32"))})


def test_debucket_accumulation_is_difference():
    # second-half accumulation bucket: interval value = cur - prev
    out = debucket(_da(10.0), _da(4.0), "acc")
    assert float(out["x"].values[0]) == pytest.approx(6.0)


def test_debucket_average_is_two_cur_minus_prev():
    # second-half average bucket: interval mean = 2*cur - prev
    out = debucket(_da(5.0), _da(3.0), "ave")
    assert float(out["x"].values[0]) == pytest.approx(7.0)
