# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Zarr datacube access mixin for cloud-hosted forcing stores.

Most analysis-ready forcing products are published as a single Zarr datacube on
object storage (ARCO-ERA5 on GCS, CONUS404 on S3, …). This mixin opens such a
store lazily with xarray so a bbox+time subset transfers only the overlapping
chunks. Heavy deps (xarray, zarr, fsspec, gcsfs) live in the optional ``climate``
extra and are imported lazily *inside* methods so the module stays importable
(and registry ``discover()`` keeps working) without the extra.
"""

from __future__ import annotations

from typing import Any

import structlog

from cfs.core.exceptions import MissingExtraError

logger = structlog.get_logger(__name__)


class ZarrStoreMixin:
    """Mixin for reading a cloud-hosted Zarr forcing datacube via xarray."""

    @staticmethod
    def _require_climate_extra() -> Any:
        try:
            import xarray as xr
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "Zarr datacube access requires the 'climate' extra: "
                "pip install -e '.[climate]' (xarray, zarr, fsspec, gcsfs)"
            ) from e
        return xr

    @staticmethod
    def _reset_threadpool_shutdown_flag() -> None:
        """Clear CPython's thread-pool shutdown flag.

        zarr v3 codecs use ``asyncio.to_thread()`` → ``ThreadPoolExecutor.submit``,
        which raises ``RuntimeError`` if ``_python_exit()`` already set the global
        shutdown flag (common when a long-lived server has finished a prior
        request on the main thread). Carried over from SYMFLUENCE's ERA5 path.
        """
        import concurrent.futures.thread as _cft

        if hasattr(_cft, "_shutdown"):
            _cft._shutdown = False
        if hasattr(_cft, "_global_shutdown"):
            _cft._global_shutdown = False

    def _open_zarr(
        self,
        store: str,
        *,
        gcs_anonymous: bool = False,
        consolidated: bool = True,
        storage_options: dict | None = None,
    ) -> Any:
        """Open a Zarr store as a lazy xarray Dataset.

        ``store`` is a path/URL. With ``gcs_anonymous`` the store is opened via an
        anonymous ``gcsfs`` mapper (public GCS buckets like ARCO-ERA5).
        """
        xr = self._require_climate_extra()
        self._reset_threadpool_shutdown_flag()

        if gcs_anonymous:
            try:
                import gcsfs
            except ImportError as e:  # pragma: no cover
                raise MissingExtraError(
                    "Anonymous GCS access needs gcsfs: pip install gcsfs"
                ) from e
            gcs = gcsfs.GCSFileSystem(token="anon")  # nosec B106 - public bucket
            mapper = gcs.get_mapper(store)
            return xr.open_zarr(mapper, consolidated=consolidated, chunks={})

        return xr.open_zarr(
            store, consolidated=consolidated, chunks={}, storage_options=storage_options
        )

    def _open_s3_zarr(
        self,
        key: str,
        *,
        anonymous: bool = True,
        consolidated: bool | None = None,
        endpoint_url: str | None = None,
    ) -> Any:
        """Open a Zarr store on S3 (or an S3-compatible endpoint) as a lazy Dataset.

        ``key`` is a ``bucket/path.zarr`` key (no ``s3://`` prefix needed).
        ``anonymous`` uses unsigned access for public buckets (NOAA Open Data,
        OSN, …). ``endpoint_url`` targets S3-compatible stores (e.g. OSN).
        """
        xr = self._require_climate_extra()
        self._reset_threadpool_shutdown_flag()
        try:
            import s3fs
        except ImportError as e:  # pragma: no cover
            raise MissingExtraError(
                "S3 Zarr access needs s3fs: pip install s3fs"
            ) from e
        client_kwargs = {"endpoint_url": endpoint_url} if endpoint_url else {}
        fs = s3fs.S3FileSystem(anon=anonymous, client_kwargs=client_kwargs)
        mapper = s3fs.S3Map(key, s3=fs)
        return xr.open_zarr(mapper, consolidated=consolidated, chunks={})
