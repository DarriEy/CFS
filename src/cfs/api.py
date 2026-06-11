# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""The blessed one-shot public API: :func:`fetch`, :func:`fetch_sync`, :func:`configure`.

This is the facade embedders (e.g. the SYMFLUENCE forcing adapter) call. It
wraps the lower-level seam — ``discover()`` / ``get_connector(slug)`` / the
async-context-manager connector protocol — into a single call that accepts
either a full product ID (``"era5_arco:single_levels"``, what the CLI takes)
or a bare provider slug (``"aorc"``, resolved when the provider offers exactly
one product).

Connector modules stay lazy: nothing under ``cfs.connectors`` is imported until
``fetch()`` calls ``discover()``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cfs.core.config import Settings, get_settings
from cfs.core.models import BoundingBox, FetchResult, TimeRange
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["configure", "fetch", "fetch_sync"]


# ── Input coercion (ergonomics for embedders) ───────────────────────


def _as_bbox(bbox: BoundingBox | Sequence[float]) -> BoundingBox:
    if isinstance(bbox, BoundingBox):
        return bbox
    min_lon, min_lat, max_lon, max_lat = (float(x) for x in bbox)
    return BoundingBox(min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat)


def _as_time(value: datetime | str) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _as_time_range(time_range: TimeRange | Sequence[datetime | str]) -> TimeRange:
    if isinstance(time_range, TimeRange):
        return time_range
    start, end = time_range
    return TimeRange(start=_as_time(start), end=_as_time(end))


def _as_variables(variables: Sequence[CanonicalVar | str] | None) -> list[CanonicalVar] | None:
    if variables is None:
        return None
    return [CanonicalVar(v) for v in variables]


# ── One-shot fetch ──────────────────────────────────────────────────


async def fetch(
    product_or_slug: str,
    bbox: BoundingBox | Sequence[float],
    time_range: TimeRange | Sequence[datetime | str],
    variables: Sequence[CanonicalVar | str] | None = None,
    config: dict | None = None,
) -> tuple[xr.Dataset, FetchResult]:
    """Acquire + subset a forcing product to a canonical gridded dataset, in one call.

    Runs the full connector lifecycle: ``discover()`` the registry, resolve the
    connector from the identifier, open it as an async context manager (with
    the optional provider-specific ``config`` dict), and fetch.

    :param product_or_slug: A full product ID ``"{provider}:{product}"`` (the
        same identifier ``cfs fetch -P`` accepts, e.g.
        ``"era5_arco:single_levels"``) — the connector is the part before the
        first ``:``. A bare provider slug (no ``:``) is also accepted and is
        resolved to the provider's product when it offers exactly one;
        otherwise a ``ValueError`` lists the candidate product IDs.
    :param bbox: A :class:`~cfs.core.models.BoundingBox`, or a
        ``(min_lon, min_lat, max_lon, max_lat)`` sequence in EPSG:4326.
    :param time_range: A :class:`~cfs.core.models.TimeRange`, or a
        ``(start, end)`` pair of ``datetime`` objects / ISO-8601 strings.
    :param variables: Canonical variables to return —
        :class:`~cfs.core.vocabulary.CanonicalVar` members or their string
        values. ``None`` means "all the product offers".
    :param config: Optional provider-specific connector configuration dict
        (e.g. ``{"members": ["gec00"]}`` for GEFS).
    :returns: ``(dataset, result)`` — the canonical-v1 ``xarray.Dataset``
        (lazy where the protocol allows) and the
        :class:`~cfs.core.models.FetchResult` describing it.
    """
    discover()
    slug = product_or_slug.split(":", 1)[0]
    try:
        conn_cls = get_connector(slug)
    except KeyError:
        raise KeyError(
            f"No connector registered for provider '{slug}'. "
            f"Known providers: {', '.join(list_providers())}"
        ) from None

    box = _as_bbox(bbox)
    tr = _as_time_range(time_range)
    req_vars = _as_variables(variables)

    async with conn_cls(config=config) as conn:
        product_id = product_or_slug
        if ":" not in product_or_slug:
            products = await conn.list_products()
            if len(products) != 1:
                ids = ", ".join(p.id for p in products)
                raise ValueError(
                    f"Provider '{slug}' offers {len(products)} products; "
                    f"pass a full product ID: {ids}"
                )
            product_id = products[0].id
        return await conn.fetch(product_id, box, tr, req_vars)


def fetch_sync(
    product_or_slug: str,
    bbox: BoundingBox | Sequence[float],
    time_range: TimeRange | Sequence[datetime | str],
    variables: Sequence[CanonicalVar | str] | None = None,
    config: dict | None = None,
) -> tuple[xr.Dataset, FetchResult]:
    """Synchronous wrapper around :func:`fetch` via ``asyncio.run``.

    For plain scripts and synchronous embedders. Raises a clear
    ``RuntimeError`` when called from inside a running event loop — in async
    code, ``await cfs.fetch(...)`` directly instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no running loop — the normal, supported case
    else:
        raise RuntimeError(
            "cfs.fetch_sync() was called from a running event loop; "
            "use 'await cfs.fetch(...)' instead."
        )
    return asyncio.run(fetch(product_or_slug, bbox, time_range, variables, config))


# ── Runtime configuration ───────────────────────────────────────────


def configure(**overrides: Any) -> Settings:
    """Override CFS runtime settings after import (cache dir, timeouts, guardrails…).

    CFS settings are env-driven (``CFS_*`` variables, see
    :class:`~cfs.core.config.Settings`) and cached on first use. This hook lets
    an embedder set them programmatically: each keyword must be a ``Settings``
    field name; its value is written to the corresponding ``CFS_<FIELD>``
    environment variable (lists become comma-separated, booleans
    ``true``/``false``), and the ``get_settings()`` cache is cleared so the
    override takes effect everywhere settings are read (connectors read them
    at call time, never at import time). Pass ``None`` to drop a previous
    override and fall back to the environment/default.

    :returns: The new effective :class:`~cfs.core.config.Settings`.

    Example::

        import cfs
        cfs.configure(cache_dir="/scratch/cfs-cache", provider_timeout_s=300)
    """
    valid = Settings.model_fields
    unknown = sorted(set(overrides) - set(valid))
    if unknown:
        raise TypeError(
            f"Unknown CFS setting(s) {', '.join(unknown)!s}; "
            f"valid settings: {', '.join(sorted(valid))}"
        )
    for key, value in overrides.items():
        env = f"CFS_{key.upper()}"
        if value is None:
            os.environ.pop(env, None)
        elif isinstance(value, bool):
            os.environ[env] = "true" if value else "false"
        elif isinstance(value, list | tuple):
            os.environ[env] = ",".join(str(item) for item in value)
        else:
            os.environ[env] = str(value)
    get_settings.cache_clear()
    return get_settings()
