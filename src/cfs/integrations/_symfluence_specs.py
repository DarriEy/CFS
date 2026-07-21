# SPDX-License-Identifier: MIT
"""Framework-independent data model for SYMFLUENCE dataset capabilities."""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple


class DatasetSpec(NamedTuple):
    """One dataset the community backend can serve."""

    family: str
    dataset_ids: tuple[str, ...]
    product: str | None
    grid: str
    variables: frozenset[str]
    fetchable: frozenset[str]
    auth: frozenset[str]
    temporal: tuple[str, str] | None
    spatial: tuple[float, float, float, float] | None
    parity: str | None
    variables_key: str | None
    native_to_canonical: dict[str, str] | None
    notes: str


def find_spec(specs: Iterable[DatasetSpec], dataset_id: str) -> DatasetSpec | None:
    """Return a case-insensitive dataset-id match from *specs*."""
    wanted = dataset_id.casefold()
    return next((spec for spec in specs if any(wanted == candidate.casefold() for candidate in spec.dataset_ids)), None)
