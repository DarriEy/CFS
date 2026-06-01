# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Provider registry — discovers and manages forcing connector plugins.

Mirrors the CAS/CSFS registry exactly so a connector is registered by importing
its module: ``@register("era5_arco")`` on the class, then ``discover()`` imports
every module under ``cfs.connectors`` to trigger the decorators.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cfs.connectors.base import BaseForcingConnector

_REGISTRY: dict[str, type[BaseForcingConnector]] = {}


def register(slug: str):
    """Decorator to register a connector class under a provider slug."""

    def wrapper(cls: type[BaseForcingConnector]) -> type[BaseForcingConnector]:
        _REGISTRY[slug] = cls
        return cls

    return wrapper


def get_connector(slug: str) -> type[BaseForcingConnector]:
    if slug not in _REGISTRY:
        raise KeyError(f"No connector registered for provider '{slug}'")
    return _REGISTRY[slug]


def list_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


def discover() -> None:
    """Import all connector modules to trigger registration."""
    import importlib
    import pkgutil

    import cfs.connectors as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name not in ("base",) and not info.ispkg:
            importlib.import_module(f"cfs.connectors.{info.name}")
