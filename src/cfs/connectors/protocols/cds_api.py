# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Copernicus Climate Data Store (CDS) API access mixin.

CDS products (ERA5-Land, CARRA, CERRA) work unlike the cloud-native stores: you
submit a request describing variables, dates, and an ``area`` bounding box; the
CDS queues it, subsets server-side, and returns a NetCDF to download. That maps
cleanly onto the CFS boundary — the *acquire-and-subset* happens through the
request's ``area`` parameter, and the connector then harmonizes the downloaded
NetCDF to the canonical schema.

This mixin encapsulates the shared CDS mechanics so each connector only declares
its dataset name, variable list, and unit mappings:

  * credential detection with an actionable error (``~/.cdsapirc`` / env vars),
  * ``area`` from a CFS BoundingBox  →  CDS ``[North, West, South, East]``,
  * monthly request chunking over a time range,
  * a retrying ``retrieve`` run in a worker thread (cdsapi is blocking),
  * opening the result, normalizing ``valid_time``→``time`` and dropping the
    singleton ``number``/``expver`` dims the new CDS backend adds.

Heavy/auth deps (``cdsapi``) live in the optional ``cds`` extra and are imported
lazily so the module stays importable for registry discovery without them.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import structlog

from cfs.core.config import get_settings
from cfs.core.exceptions import MissingExtraError, RegistrationRequiredError
from cfs.core.models import BoundingBox, TimeRange

logger = structlog.get_logger(__name__)

_CDS_REGISTER_URL = "https://cds.climate.copernicus.eu/how-to-api"


class CDSAPIMixin:
    """Mixin providing Copernicus CDS request/retrieve plumbing."""

    slug: str

    # ── Credentials ─────────────────────────────────────────────────

    def _cds_credentials_present(self) -> bool:
        if os.path.exists(os.path.expanduser("~/.cdsapirc")):
            return True
        return bool(os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"))

    def _cds_client(self) -> Any:
        """Construct a cdsapi client, raising a clear, actionable error first."""
        try:
            import cdsapi
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "CDS access requires the 'cds' extra: pip install -e '.[cds]' (cdsapi)"
            ) from e
        if not self._cds_credentials_present():
            raise RegistrationRequiredError(
                self.slug,
                _CDS_REGISTER_URL,
                "No CDS credentials found. Create ~/.cdsapirc with:\n"
                "  url: https://cds.climate.copernicus.eu/api\n"
                "  key: <YOUR_API_KEY>\n"
                "or set CDSAPI_URL and CDSAPI_KEY. Accept the dataset licence on "
                "the CDS website before first use.",
            )
        # quiet=True: we do our own logging; cdsapi reads ~/.cdsapirc / env itself.
        return cdsapi.Client(quiet=True, wait_until_complete=True)

    # ── Request construction ────────────────────────────────────────

    @staticmethod
    def _cds_area(bbox: BoundingBox) -> list[float]:
        """CFS bbox → CDS ``area`` = [North, West, South, East]."""
        return [bbox.max_lat, bbox.min_lon, bbox.min_lat, bbox.max_lon]

    @staticmethod
    def _month_chunks(time_range: TimeRange) -> list[tuple[int, int]]:
        """Enumerate (year, month) pairs spanning the request, inclusive."""
        y, m = time_range.start.year, time_range.start.month
        end_y, end_m = time_range.end.year, time_range.end.month
        out: list[tuple[int, int]] = []
        while (y, m) <= (end_y, end_m):
            out.append((y, m))
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out

    @staticmethod
    def _all_days() -> list[str]:
        return [f"{d:02d}" for d in range(1, 32)]

    @staticmethod
    def _hourly_times(step_hours: int = 1) -> list[str]:
        return [f"{h:02d}:00" for h in range(0, 24, step_hours)]

    # ── Retrieve + open ─────────────────────────────────────────────

    async def _cds_retrieve(self, dataset: str, request: dict, target: Path) -> Path:
        """Run a (blocking) cdsapi retrieve in a worker thread, with retries."""
        if target.exists() and target.stat().st_size > 0:
            logger.debug("cds cache hit", path=str(target))
            return target

        client = self._cds_client()
        attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                tmp = target.with_suffix(target.suffix + ".part")
                await asyncio.to_thread(client.retrieve, dataset, request, str(tmp))
                tmp.replace(target)
                return target
            except Exception as e:  # noqa: BLE001 - retry transient CDS/queue errors
                last_exc = e
                logger.warning(
                    "cds retrieve failed", dataset=dataset, attempt=attempt, error=str(e)
                )
                if attempt < attempts:
                    await asyncio.sleep(min(60.0, 5.0 * 2 ** attempt))
        raise RuntimeError(f"CDS retrieve failed for {dataset} after {attempts} attempts: {last_exc}")

    def _cds_cache_dir(self) -> Path:
        d = Path(os.path.expanduser(get_settings().cache_dir)) / self.slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _cds_open(path: Path) -> Any:
        """Open a CDS NetCDF and normalize coords to the CFS convention.

        Renames ``valid_time``→``time`` (new CDS backend) and squeezes the
        singleton ``number``/``expver`` dimensions it adds, so harmonization
        sees a clean (time, latitude, longitude) cube.
        """
        import xarray as xr

        ds = xr.open_dataset(path)
        if "valid_time" in ds.dims or "valid_time" in ds.coords:
            ds = ds.rename({"valid_time": "time"})
        for extra in ("number", "expver"):
            if extra in ds.dims and ds.sizes.get(extra, 1) == 1:
                ds = ds.squeeze(extra, drop=True)
            elif extra in ds.coords and extra not in ds.dims:
                ds = ds.drop_vars(extra, errors="ignore")
        return ds
