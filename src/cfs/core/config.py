# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Central runtime configuration for CFS.

All settings are read from environment variables prefixed with ``CFS_``
(e.g. ``CFS_PROVIDER_TIMEOUT_S``, ``CFS_MAX_CELLS_PER_FETCH``). Mirrors the CAS
config pattern so the same image runs as an internal tool or a public service.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CFS_", extra="ignore")

    # ── Timeouts (seconds) ──────────────────────────────────────────
    provider_timeout_s: float = 120.0
    """Per-provider store-open / subset deadline."""
    request_timeout_s: float = 600.0
    """Whole-request backstop deadline."""

    # ── Subset guardrails ───────────────────────────────────────────
    max_cells_per_fetch: int = 50_000_000
    """Refuse fetches whose (time x lat x lon) cell count exceeds this, to stop
    an accidental continental + decadal pull. 0 disables the guard."""
    max_area_deg2: float = 400.0
    """Refuse a bbox larger than this many square degrees (0 disables)."""

    # ── Materialization ─────────────────────────────────────────────
    default_lazy: bool = True
    """Return dask-backed lazy datasets by default; caller decides to .load()."""

    # ── Concurrency ─────────────────────────────────────────────────
    fetch_concurrency: int = 8
    """Max per-file opens a connector runs in parallel. Per-hour/day/year stores
    (NLDAS, HRRR, MERRA-2, Daymet, GPM, CHIRPS) pay one network round-trip per
    file; opening them concurrently (bounded by this) turns N serial latencies
    into ceil(N / fetch_concurrency). 1 forces the old serial behaviour."""

    # ── Quality control ─────────────────────────────────────────────
    qc_enabled: bool = True
    """Sample harmonized cubes against canonical valid ranges and surface
    out-of-range warnings in FetchResult.warnings (catches unit-conversion
    bugs). Advisory only — never fails a fetch."""

    # ── Local cache (for HTTP-download sources without server subsetting) ──
    cache_dir: str = "~/.cache/cfs"
    """Where connectors that must download whole files (contiguous/netCDF-3 HTTP
    sources, via HTTPFilesMixin._download_cached) cache them, to avoid
    re-downloading across fetches. Range-capable HDF5 sources (CHIRPS) stream
    only the needed chunks and never touch this."""

    # ── CORS / auth (off by default, like CAS) ──────────────────────
    cors_origins: Annotated[list[str], NoDecode] = ["*"]
    auth_enabled: bool = False
    api_keys: Annotated[list[str], NoDecode] = []

    @field_validator("cors_origins", "api_keys", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                import json

                return json.loads(s)
            return [item.strip() for item in s.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
