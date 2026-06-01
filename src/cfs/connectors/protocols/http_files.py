# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""HTTP whole-file download mixin for non-cloud-optimized sources.

Some forcing archives are plain HTTP NetCDF files with no server-side subsetting
(CHIRPS yearly globals, …). For these CFS cannot subset-on-demand — it must
download the covering file(s), cache them, then subset locally. The result still
honours the CFS boundary (a canonical, bbox+time subset dataset), but such a
fetch is necessarily ``lazy=False`` and heavier than a cloud-native one.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from cfs.core.config import get_settings
from cfs.core.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


class HTTPFilesMixin:
    """Download-and-cache helper for plain-HTTP file sources."""

    slug: str

    def _cache_dir(self) -> Path:
        d = Path(os.path.expanduser(get_settings().cache_dir)) / self.slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _download_cached(self, url: str, filename: str, *, force: bool = False) -> Path:
        """Download ``url`` to the per-provider cache as ``filename`` (skip if present)."""
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise ConnectorError(self.slug, "HTTP sources need 'requests' installed") from e

        dest = self._cache_dir() / filename
        if dest.exists() and dest.stat().st_size > 0 and not force:
            logger.debug("cache hit", path=str(dest))
            return dest

        logger.info("downloading", url=url, dest=str(dest))
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=get_settings().provider_timeout_s) as r:
            if r.status_code != 200:
                raise ConnectorError(self.slug, f"HTTP {r.status_code} for {url}")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
        tmp.replace(dest)
        return dest
