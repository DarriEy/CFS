# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Humidity conversions — relative humidity → specific humidity.

CARRA and CERRA provide 2 m relative humidity (%) rather than specific humidity.
We recover specific humidity via the saturation vapour pressure (Bolton 1980
Magnus form) and the standard mixing-ratio relation. Inputs may be plain floats
or xarray DataArrays (the operations are elementwise numpy ufuncs, so dask-backed
arrays stay lazy).

References
----------
Bolton, D. (1980), The computation of equivalent potential temperature,
*Mon. Wea. Rev.* 108, 1046-1053 — eq. for saturation vapour pressure.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# Ratio of the gas constants of dry air to water vapour (Rd/Rv = Mw/Md).
_EPSILON = 0.62198
_ONE_MINUS_EPS = 1.0 - _EPSILON


def saturation_vapor_pressure(temperature_k: Any) -> Any:
    """Saturation vapour pressure over water, in Pa, from temperature in K.

    Bolton (1980): ``e_s = 611.2 * exp(17.67 * Tc / (Tc + 243.5))`` with Tc in °C.
    """
    tc = temperature_k - 273.15
    return 611.2 * np.exp(17.67 * tc / (tc + 243.5))


def specific_humidity_from_rh(
    relative_humidity_pct: Any,
    temperature_k: Any,
    pressure_pa: Any,
) -> Any:
    """Specific humidity (kg kg-1) from RH (%), temperature (K), pressure (Pa).

    ``e = (RH/100) * e_s(T)``  then  ``q = eps * e / (p - (1-eps) * e)``.
    """
    e_s = saturation_vapor_pressure(temperature_k)
    e = (relative_humidity_pct / 100.0) * e_s
    return _EPSILON * e / (pressure_pa - _ONE_MINUS_EPS * e)


def dewpoint_from_vapor_pressure(vapor_pressure_pa: Any) -> Any:
    """Dewpoint temperature (K) from actual vapour pressure (Pa).

    Inverts the Bolton saturation formula (``e = 611.2 * exp(17.67*Td/(Td+243.5))``)
    for the dewpoint — needs only vapour pressure, no surface pressure. Used by
    Daymet, which ships vapour pressure but not dewpoint or specific humidity.
    """
    ln = np.log(vapor_pressure_pa / 611.2)
    tc = 243.5 * ln / (17.67 - ln)
    return tc + 273.15
