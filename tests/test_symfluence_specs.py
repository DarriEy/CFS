# SPDX-License-Identifier: MIT
from cfs.integrations._symfluence_specs import DatasetSpec, find_spec


def _spec(*ids: str) -> DatasetSpec:
    return DatasetSpec(
        "test",
        ids,
        "test:product",
        "regular_latlon",
        frozenset(),
        frozenset(),
        frozenset(),
        None,
        None,
        None,
        None,
        None,
        "",
    )


def test_find_spec_matches_alias_case_insensitively():
    expected = _spec("NLDAS", "NLDAS-2")
    assert find_spec((expected,), "nLdAs-2") is expected


def test_find_spec_returns_none_for_unknown_id():
    assert find_spec((_spec("ERA5"),), "unknown") is None
