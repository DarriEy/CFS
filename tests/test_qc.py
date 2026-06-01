# SPDX-License-Identifier: MIT
"""QC range-warning tests — the unit-error guard."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.core.vocabulary import CanonicalVar
from cfs.qc import sample_range_warnings


def _cube(varname, value):
    return xr.Dataset(
        {varname: (("time", "latitude", "longitude"), np.full((2, 2, 2), value))},
        coords={"time": [0, 1], "latitude": [0.0, 1.0], "longitude": [0.0, 1.0]},
    )


def test_in_range_no_warning():
    ds = _cube(str(CanonicalVar.AIR_TEMPERATURE), 290.0)
    assert sample_range_warnings(ds) == []


def test_celsius_temperature_flagged():
    # 20 (°C, unconverted) is way below the 180 K floor → flagged.
    ds = _cube(str(CanonicalVar.AIR_TEMPERATURE), 20.0)
    w = sample_range_warnings(ds)
    assert len(w) == 1 and "air_temperature" in w[0]


def test_unconverted_precip_flagged():
    # 8.64 (mm/day, not divided by 86400) >> 0.1 kg m-2 s-1 ceiling → flagged.
    ds = _cube(str(CanonicalVar.PRECIPITATION_FLUX), 8.64)
    w = sample_range_warnings(ds)
    assert len(w) == 1 and "precipitation_flux" in w[0]


def test_converted_precip_ok():
    ds = _cube(str(CanonicalVar.PRECIPITATION_FLUX), 1e-4)
    assert sample_range_warnings(ds) == []


def test_non_canonical_var_ignored():
    ds = _cube("some_random_var", 99999.0)
    assert sample_range_warnings(ds) == []


def test_all_nan_flagged():
    ds = _cube(str(CanonicalVar.AIR_TEMPERATURE), np.nan)
    w = sample_range_warnings(ds)
    assert len(w) == 1 and "NaN" in w[0]
