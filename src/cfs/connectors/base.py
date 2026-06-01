# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Abstract base class for all CFS forcing connectors.

The contract is deliberately small and defines the CFS boundary precisely:

  * ``list_products()`` — what this provider offers (catalog metadata)
  * ``fetch(...)``      — acquire + subset → a **canonical** ``xarray.Dataset``

A connector's job ends at a harmonized, bbox-and-time subset cube. It must NOT
remap to HRUs, write model-specific schemas, or serialize to disk — those are
the consumer's responsibility. Connectors should return lazy (dask-backed)
datasets so the caller decides when to materialize.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

from cfs.core.config import Settings, get_settings
from cfs.core.exceptions import ConnectorError, SubsetError
from cfs.core.models import BoundingBox, FetchResult, ForcingProduct, TimeRange
from cfs.core.vocabulary import CanonicalVar

if TYPE_CHECKING:
    import xarray as xr

logger = structlog.get_logger()


class BaseForcingConnector(ABC):
    """Interface every forcing provider connector must implement."""

    slug: str
    display_name: str
    base_url: str
    protocol: str

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    async def __aenter__(self) -> BaseForcingConnector:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    # ── Abstract contract ───────────────────────────────────────────

    @abstractmethod
    async def list_products(self) -> list[ForcingProduct]:
        """Return the catalog of forcing products this provider offers."""

    @abstractmethod
    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        """Acquire and subset a product to a canonical gridded dataset.

        Returns ``(dataset, result)`` where ``dataset`` carries canonical
        variable names/units (see :mod:`cfs.core.vocabulary`) over the requested
        bbox and time range, and ``result`` is the :class:`FetchResult`
        describing its provenance and shape.
        """

    # ── Optional overrides ──────────────────────────────────────────

    async def check_health(self) -> bool:
        """Lightweight liveness probe. Override per provider."""
        return True

    # ── Helpers ─────────────────────────────────────────────────────

    def _require_product(self, product_id: str, products: list[ForcingProduct]) -> ForcingProduct:
        for p in products:
            if p.id == product_id:
                return p
        raise ConnectorError(self.slug, f"Unknown product '{product_id}'")

    # ── Shared guardrails + result assembly (hardening) ─────────────

    @staticmethod
    def _guard_area(bbox: BoundingBox, settings: Settings) -> None:
        """Reject a bbox larger than ``CFS_MAX_AREA_DEG2`` (antimeridian-safe)."""
        width = (bbox.max_lon - bbox.min_lon) % 360.0 or 360.0
        area = width * (bbox.max_lat - bbox.min_lat)
        if settings.max_area_deg2 and area > settings.max_area_deg2:
            raise SubsetError(
                f"bbox area {area:.1f} deg^2 exceeds CFS_MAX_AREA_DEG2 "
                f"({settings.max_area_deg2}); narrow the bounding box."
            )

    def _finalize(
        self,
        canonical: xr.Dataset,
        *,
        product: ForcingProduct,
        bbox: BoundingBox,
        time_range: TimeRange,
        provenance: str,
        t0: float,
        settings: Settings | None = None,
        lazy: bool = True,
        extra_warnings: list[str] | None = None,
        ydim: str = "latitude",
        xdim: str = "longitude",
    ) -> FetchResult:
        """Apply the cell-count guard + QC and build the FetchResult.

        Centralizes the shape/guard/QC logic every connector needs so the
        guardrails (and the QC unit-error check) are enforced uniformly. For a
        regular grid the spatial dims are ``latitude``/``longitude`` (the
        defaults); for a 2-D/projected grid (RDRS rlat/rlon, CONUS404 y/x) pass
        ``ydim``/``xdim`` so the cell count reflects the native index dims.
        """
        settings = settings or get_settings()
        n_time = int(canonical.sizes.get("time", 0))
        n_lat = int(canonical.sizes.get(ydim, 0))
        n_lon = int(canonical.sizes.get(xdim, 0))
        cells = n_time * n_lat * n_lon
        if settings.max_cells_per_fetch and cells > settings.max_cells_per_fetch:
            raise SubsetError(
                f"Fetch of {cells:,} cells exceeds CFS_MAX_CELLS_PER_FETCH "
                f"({settings.max_cells_per_fetch:,}); narrow the bbox or time range."
            )

        warnings = list(extra_warnings or [])
        if settings.qc_enabled:
            try:
                from cfs.qc import sample_range_warnings

                warnings += sample_range_warnings(canonical)
            except Exception as e:  # QC is advisory — never break a fetch
                logger.debug("qc skipped", provider=self.slug, error=str(e))

        return FetchResult(
            product_id=product.id,
            provider=self.slug,
            variables=[CanonicalVar(str(v)) for v in canonical.data_vars],
            bbox=bbox,
            time_range=time_range,
            n_times=n_time,
            n_lat=n_lat,
            n_lon=n_lon,
            resolution_deg=product.resolution_deg,
            lazy=lazy,
            provenance=provenance,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings,
        )
