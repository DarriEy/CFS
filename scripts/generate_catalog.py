#!/usr/bin/env python3
"""Regenerate provider-count and connector-table documentation from inventory."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "inventory" / "providers.yaml"
README = ROOT / "README.md"
CATALOG = ROOT / "docs" / "catalog.md"
INDEX = ROOT / "docs" / "index.md"

TABLE_START = "<!-- BEGIN GENERATED PROVIDER TABLE -->"
TABLE_END = "<!-- END GENERATED PROVIDER TABLE -->"


def load_inventory() -> list[dict]:
    providers = yaml.safe_load(INVENTORY.read_text())
    if not isinstance(providers, list) or not providers:
        raise ValueError("inventory/providers.yaml must contain a non-empty list")
    required = {"slug", "name", "grid", "protocol", "auth", "status", "verified"}
    for provider in providers:
        missing = required - provider.keys()
        if missing:
            raise ValueError(f"{provider.get('slug', '<unknown>')}: missing {sorted(missing)}")
    return providers


def verification(provider: dict) -> str:
    if provider["verified"]:
        return "live (creds)" if provider["auth"] != "anonymous" else "live"
    if provider["status"] == "implemented_blocked_access":
        return "blocked access"
    return "offline"


def provider_table(providers: list[dict]) -> str:
    rows = [
        "| slug | product | grid | access | auth | verified |",
        "|------|---------|------|--------|------|----------|",
    ]
    for p in providers:
        rows.append(
            f"| `{p['slug']}` | {p['name']} | {p['grid']} | {p['protocol']} | "
            f"{p['auth']} | {verification(p)} |"
        )
    return "\n".join(rows)


def replace_table(text: str, table: str) -> str:
    generated = f"{TABLE_START}\n{table}\n{TABLE_END}"
    if TABLE_START in text:
        return re.sub(
            rf"{re.escape(TABLE_START)}.*?{re.escape(TABLE_END)}",
            generated,
            text,
            flags=re.DOTALL,
        )
    start = text.index("| slug | product | grid | access | auth | verified |")
    end = text.index('\n\n"live" means', start)
    return text[:start] + generated + text[end:]


def render() -> tuple[str, str, str, str]:
    providers = load_inventory()
    total = len(providers)
    live = sum(bool(p["verified"]) for p in providers)
    pending = total - live

    readme = README.read_text()
    readme = re.sub(r"one async interface over \d+ products", f"one async interface over {total} providers", readme)
    readme = re.sub(
        r"\d+ connectors — \d+ live-verified against their upstream stores .*?; `mswep` and\n"
        r"`em_earth` are offline-verified pending access/credentials\.",
        f"{total} connectors — {live} live-verified against their upstream stores; "
        f"{pending} remain offline or access-blocked.",
        readme,
        flags=re.DOTALL,
    )

    catalog = CATALOG.read_text()
    catalog = re.sub(
        r"CFS ships \*\*\d+ connectors\*\* — \d+ live-verified against their upstream stores\n"
        r"\(the auth-gated ones with real CDS and Earthdata credentials\), \d+\n"
        r"offline-verified pending access or provider-specific credentials\.",
        f"CFS ships **{total} connectors** — {live} live-verified against their upstream stores "
        f"and {pending} offline or access-blocked. Counts and the table below are generated from "
        "`inventory/providers.yaml`.",
        catalog,
    )
    catalog = replace_table(catalog, provider_table(providers))
    index = INDEX.read_text()
    index = re.sub(
        r"One asynchronous interface to \d+ meteorological forcing products",
        f"One asynchronous interface to {total} meteorological forcing providers",
        index,
    )
    index = re.sub(r"\*\*\d+ connectors\*\*", f"**{total} connectors**", index)
    header = f"# Total providers: {total} — {live} live-verified + {pending} offline/access-gated."
    return readme, catalog, index, header


def main() -> None:
    readme, catalog, index, header = render()
    README.write_text(readme)
    CATALOG.write_text(catalog)
    INDEX.write_text(index)
    inventory = INVENTORY.read_text()
    inventory = re.sub(r"# Total providers:.*", header, inventory, count=1)
    INVENTORY.write_text(inventory)


if __name__ == "__main__":
    main()
