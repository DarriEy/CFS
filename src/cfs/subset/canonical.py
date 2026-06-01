# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Harmonize a provider's native cube to the CFS canonical schema.

A connector supplies a list of :class:`VariableMapping` rows (native name →
canonical name + a linear unit conversion). This module applies them: renames
variables, converts units, attaches CF metadata, and standardizes coordinate
names. The output is the canonical, model-agnostic dataset where CFS stops.

Conversions are linear (``canonical = source * scale + offset``), which covers
every common forcing case — including the two that bite everyone:
  * accumulation → rate  (e.g. ERA5 hourly precip in m → kg m-2 s-1)
  * energy accumulation → flux  (e.g. ERA5 J m-2 over 1 h → W m-2)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from cfs.core.exceptions import HarmonizationError
from cfs.core.vocabulary import CANONICAL, CanonicalVar

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class VariableMapping:
    """One native variable's mapping to the canonical schema.

    The transform is applied in order: optional reset-aware de-accumulation
    (for running totals like ERA5-Land precip/radiation), then the linear
    ``canonical = source * scale + offset`` to the canonical units defined in
    :data:`cfs.core.vocabulary.CANONICAL`.
    """

    source_name: str
    canonical: CanonicalVar
    scale: float = 1.0
    offset: float = 0.0
    deaccumulate: bool = False
    note: str = ""


def harmonize(
    ds: Any,
    mappings: list[VariableMapping],
    requested: list[CanonicalVar] | None = None,
    *,
    lat_name: str = "latitude",
    lon_name: str = "longitude",
    time_name: str = "time",
) -> Any:
    """Return a new dataset with canonical variable names, units, and metadata.

    Only variables present in ``ds`` and (if given) in ``requested`` are kept.
    Stays lazy: arithmetic on dask-backed arrays does not trigger compute.
    """
    wanted = set(requested) if requested else None
    selected = [
        m for m in mappings
        if m.source_name in ds.data_vars and (wanted is None or m.canonical in wanted)
    ]
    if not selected:
        raise HarmonizationError(
            f"None of the product's variables matched the request. "
            f"Available: {list(ds.data_vars)}; requested: "
            f"{[v.value for v in requested] if requested else 'all'}"
        )

    rename: dict[str, str] = {}
    out = ds
    for m in selected:
        spec = CANONICAL[m.canonical]
        da = ds[m.source_name]
        if m.deaccumulate:
            from cfs.subset.deaccumulate import deaccumulate as _deaccum

            da = _deaccum(da, time_name=time_name)
        if m.scale != 1.0 or m.offset != 0.0:
            da = da * m.scale + m.offset
        da.attrs = {
            "standard_name": str(m.canonical),
            "units": spec.units,
            "long_name": spec.description,
            "cfs_source_name": m.source_name,
        }
        if m.note:
            da.attrs["cfs_conversion"] = m.note
        out = out.assign({m.source_name: da})
        rename[m.source_name] = str(m.canonical)

    keep = [m.source_name for m in selected]
    out = out[keep].rename(rename)

    # Standardize coordinate names to the canonical trio.
    coord_rename = {}
    for src, canon in ((lat_name, "latitude"), (lon_name, "longitude"), (time_name, "time")):
        if src in out.coords and src != canon and canon not in out.coords:
            coord_rename[src] = canon
    if coord_rename:
        out = out.rename(coord_rename)

    out.attrs["cfs_schema"] = "canonical-v1"
    logger.debug("harmonized to canonical schema", variables=list(rename.values()))
    return out
