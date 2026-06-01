# SPDX-License-Identifier: MIT
"""RH→specific-humidity derivation tests (Bolton/Magnus), no network."""

from __future__ import annotations

import pytest

from cfs.derive.humidity import saturation_vapor_pressure, specific_humidity_from_rh


def test_saturation_vapor_pressure_at_20c():
    # Bolton 1980 at 20 °C ≈ 2338 Pa.
    es = saturation_vapor_pressure(293.15)
    assert es == pytest.approx(2338.0, rel=0.01)


def test_saturation_increases_with_temperature():
    assert saturation_vapor_pressure(303.15) > saturation_vapor_pressure(283.15)


def test_specific_humidity_typical_value():
    # 20 °C, 50% RH, 1013.25 hPa → ~7.2 g/kg.
    q = specific_humidity_from_rh(50.0, 293.15, 101325.0)
    assert q == pytest.approx(0.00722, rel=0.02)


def test_specific_humidity_zero_rh_is_zero():
    assert specific_humidity_from_rh(0.0, 293.15, 101325.0) == pytest.approx(0.0)


def test_specific_humidity_in_canonical_range():
    # Across a plausible envelope, q stays within the canonical valid range.
    from cfs.core.vocabulary import CANONICAL, CanonicalVar

    lo, hi = CANONICAL[CanonicalVar.SPECIFIC_HUMIDITY].valid_range
    for t in (250.0, 280.0, 300.0):
        for rh in (10.0, 60.0, 100.0):
            q = specific_humidity_from_rh(rh, t, 101325.0)
            assert lo <= q <= hi
