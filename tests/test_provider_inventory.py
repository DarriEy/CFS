# SPDX-License-Identifier: MIT
"""Provider inventory is the single source of truth for public catalog metadata."""

from pathlib import Path

import yaml

from cfs.core.registry import discover, list_providers

ROOT = Path(__file__).resolve().parents[1]


def test_inventory_matches_registry_exactly():
    inventory = yaml.safe_load((ROOT / "inventory/providers.yaml").read_text())
    slugs = [provider["slug"] for provider in inventory]
    assert len(slugs) == len(set(slugs)), "provider inventory contains duplicate slugs"
    discover()
    assert slugs == list_providers() or set(slugs) == set(list_providers())


def test_generated_provider_docs_are_current():
    import importlib.util

    spec = importlib.util.spec_from_file_location("generate_catalog", ROOT / "scripts/generate_catalog.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    expected_readme, expected_catalog, expected_index, expected_header = module.render()
    assert (ROOT / "README.md").read_text() == expected_readme
    assert (ROOT / "docs/catalog.md").read_text() == expected_catalog
    assert (ROOT / "docs/index.md").read_text() == expected_index
    assert expected_header in (ROOT / "inventory/providers.yaml").read_text()
