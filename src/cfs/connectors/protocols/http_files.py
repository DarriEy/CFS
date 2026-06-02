# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""HTTP file-access mixin for archives served as plain files (no DAP/Zarr).

Some forcing archives are published as plain HTTP NetCDF files with no
server-side subsetting (CHIRPS yearly globals, …). This mixin offers two ways in:

* :meth:`_open_http_lazy` — for **chunked netCDF-4/HDF5** files on a range-capable
  server (``Accept-Ranges: bytes``). fsspec's HTTP filesystem + the h5netcdf
  engine read only the chunks overlapping a later bbox/time selection, so a
  basin-scale subset transfers a few MB instead of the whole multi-GB file. This
  is the preferred path (CHIRPS daily globals are HDF5, chunked ~20×112×400).
* :meth:`_download_cached` — fallback for **contiguous/netCDF-3** files or servers
  without range support: download the covering file(s) to the cache, then subset
  locally. Necessarily heavier and ``lazy=False``.

Either way the result honours the CFS boundary (a canonical, bbox+time subset).
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from cfs.core.config import get_settings
from cfs.core.exceptions import ConnectorError, MissingExtraError

logger = structlog.get_logger(__name__)


class HTTPFilesMixin:
    """HTTP access helpers (lazy byte-range open + download-and-cache) for file sources."""

    slug: str

    def _open_http_lazy(self, url: str):
        """Open a remote chunked netCDF-4/HDF5 file lazily over HTTP byte-range.

        Returns a dask-backed :class:`xarray.Dataset` whose chunks are fetched on
        demand, so a subsequent ``.sel(bbox+time)`` transfers only the overlapping
        chunks. The server must honour range requests; the file must be HDF5
        (use :meth:`_download_cached` for contiguous/netCDF-3 sources).
        """
        try:
            import fsspec
            import xarray as xr
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "HTTP byte-range access needs the 'climate' extra "
                "(fsspec, aiohttp, h5netcdf): pip install -e '.[climate]'"
            ) from e
        fobj = fsspec.open(url, mode="rb").open()
        return xr.open_dataset(fobj, engine="h5netcdf", chunks={})

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
