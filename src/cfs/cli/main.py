# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CFS command-line interface."""

from __future__ import annotations

import asyncio

import click
import structlog

structlog.configure(processors=[structlog.dev.ConsoleRenderer()])


@click.group()
@click.version_option(package_name="cfs")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """CFS — Community Forcing Service."""
    ctx.ensure_object(dict)


@cli.command()
def providers() -> None:
    """List registered forcing providers."""
    from cfs.core.registry import discover, list_providers

    discover()
    for slug in list_providers():
        click.echo(slug)


@cli.command()
@click.option("--provider", "-p", default=None, help="Limit to one provider slug.")
def products(provider: str | None) -> None:
    """List available forcing products (catalog metadata)."""
    from cfs.core.registry import discover, get_connector, list_providers

    discover()
    slugs = [provider] if provider else list_providers()

    async def _run() -> None:
        for slug in slugs:
            conn_cls = get_connector(slug)
            async with conn_cls() as conn:
                for p in await conn.list_products():
                    vars_ = ", ".join(v.canonical for v in p.variables)
                    click.echo(f"{p.id}  [{p.resolution_deg}°, {p.temporal.resolution}]  {vars_}")

    asyncio.run(_run())


@cli.command()
@click.option("--product", "-P", "product_id", required=True, help="Product ID, e.g. era5_arco:single_levels")
@click.option("--bbox", "-b", required=True, help="min_lon,min_lat,max_lon,max_lat")
@click.option("--start", required=True, help="Start time (ISO 8601)")
@click.option("--end", required=True, help="End time (ISO 8601)")
@click.option("--variables", "-v", default=None, help="Comma-separated canonical variable names (default: all)")
@click.option("--output", "-o", default=None, help="Write the canonical cube to this NetCDF path")
@click.option("--load/--lazy", default=False, help="Materialize the cube before reporting (default: lazy)")
def fetch(product_id, bbox, start, end, variables, output, load) -> None:
    """Acquire + subset a product to a canonical gridded dataset.

    Prints the FetchResult metadata as JSON. With --output, also writes the
    canonical NetCDF (a convenience for inspection — model-schema writing is the
    consumer's job, not CFS's).
    """
    from datetime import datetime

    from cfs.core.models import BoundingBox, TimeRange
    from cfs.core.registry import discover, get_connector
    from cfs.core.vocabulary import CanonicalVar

    discover()
    provider_slug = product_id.split(":", 1)[0]
    conn_cls = get_connector(provider_slug)

    lo, la, ho, ha = (float(x) for x in bbox.split(","))
    box = BoundingBox(min_lon=lo, min_lat=la, max_lon=ho, max_lat=ha)
    tr = TimeRange(start=datetime.fromisoformat(start), end=datetime.fromisoformat(end))
    req_vars = [CanonicalVar(v.strip()) for v in variables.split(",")] if variables else None

    async def _run() -> None:
        async with conn_cls() as conn:
            ds, result = await conn.fetch(product_id, box, tr, req_vars)
            if load or output:
                ds = ds.load()
                result.lazy = False
            if output:
                ds.to_netcdf(output)
                click.echo(f"Wrote canonical cube to {output}")
            click.echo(result.model_dump_json(indent=2))

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
