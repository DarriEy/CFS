# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Lightweight quality control for canonical forcing cubes.

A canonical variable carries a physical ``valid_range`` (see
:mod:`cfs.core.vocabulary`). After harmonization, a value far outside that range
almost always means a unit-conversion bug — a precip flux of 8.6 instead of
1e-4 (mm/day not divided by 86400), a temperature of 20 instead of 293 (°C not
converted to K). This module samples the harmonized cube and emits *warnings*
(never raises) so such mistakes surface in ``FetchResult.warnings`` instead of
silently corrupting a model run.

To stay cheap on lazy/large cubes it samples a single time step by default;
that's enough to catch gross unit errors without materializing the whole cube.
QC is advisory: callers wrap it so a QC failure can never break a fetch.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog

from cfs.core.vocabulary import CANONICAL, CanonicalVar

logger = structlog.get_logger(__name__)


def sample_range_warnings(ds: Any, *, sample: bool = True, time_name: str = "time") -> list[str]:
    """Return one warning per canonical variable whose sampled values fall
    outside its physical ``valid_range``. Empty list = everything plausible."""
    warnings: list[str] = []
    for name in ds.data_vars:
        try:
            cv = CanonicalVar(str(name))
        except ValueError:
            continue  # not a canonical variable; skip
        spec = CANONICAL.get(cv)
        if spec is None or spec.valid_range is None:
            continue

        da = ds[name]
        if sample and time_name in da.dims and da.sizes.get(time_name, 0) > 1:
            da = da.isel({time_name: 0})

        try:
            arr = np.asarray(da.values, dtype="float64")
        except Exception as e:  # pragma: no cover - defensive: QC must not crash
            logger.debug("qc sample failed", variable=str(cv), error=str(e))
            continue

        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            warnings.append(f"{cv}: all sampled values are NaN/missing")
            continue

        lo, hi = spec.valid_range
        vmin, vmax = float(finite.min()), float(finite.max())
        if vmin < lo or vmax > hi:
            warnings.append(
                f"{cv}: sampled range [{vmin:.4g}, {vmax:.4g}] falls outside the "
                f"canonical valid range {spec.valid_range} {spec.units} — "
                f"likely a unit-conversion error in the connector mapping"
            )
    return warnings
