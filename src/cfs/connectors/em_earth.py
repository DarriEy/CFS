# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""EM-Earth connector — global daily met reanalysis (S3 or FRDR HTTPS).

EM-Earth (Tang et al. 2022) is a 0.1° global daily product (1950-2019) merging
gauge + reanalysis, in deterministic and probabilistic variants, stored as
per-variable, per-month NetCDFs.

Three access routes (``config={"source": ..., "data_dir": ...}``):

1. **S3** (``source: "s3"``): the ``emearth`` bucket allows anonymous
   *listing* but *denies anonymous GET* (every region, even requester-pays) —
   it needs AWS credentials. The connector defaults to anonymous and raises a
   clear error pointing at that; pass ``config={"anon": False}`` to use the
   standard AWS credential chain once you have bucket access. (SYMFLUENCE's
   handler assumed anonymous access and silently produced "no data" on 403s.)

2. **FRDR HTTPS** (``source: "frdr"``): the authoritative FRDR archive
   (DOI 10.20383/102.0547) serves every file over *anonymous* per-file HTTPS
   (live-verified 2026-06-12). The relative layout under ``EM_Earth_v1/``
   matches the S3 layout under ``nc/`` exactly, so the same keys resolve.
   Files are whole-month globals (~100-300 MB each); they are cached in
   ``data_dir`` when given. Deterministic-daily only — the probabilistic
   archive on FRDR is split by continent/member and does not mirror the S3
   keys.

3. **Local staging** (``data_dir``): pre-staged files (FRDR/Globus downloads)
   are picked up from ``<data_dir>/<variant_folder>/<var>/<filename>`` or flat
   ``<data_dir>/<filename>`` before any network access, regardless of source.

Precip units: EM-Earth daily ``prcp`` is mm/day (→ ``/86400``) — **verified**
2026-06-12 against the FRDR deterministic daily file's own attrs
(``units: 'mm day-1'``). Note the files also carry ``prcp_corrected`` (PBCOR
WorldClim-corrected); this connector ships the raw ``prcp``, matching the
native SYMFLUENCE handler. Temperatures are °C → +273.15 (QC-protected).
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.core.config import get_settings
from cfs.core.exceptions import MissingExtraError, RegistrationRequiredError, SubsetError
from cfs.core.models import (
    BoundingBox,
    FetchResult,
    ForcingProduct,
    ProductVariable,
    Protocol,
    TemporalExtent,
    TemporalResolution,
    TimeRange,
)
from cfs.core.registry import register
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

BUCKET = "emearth"
# variant -> archive folder; S3 prefixes it with "nc/", FRDR with "EM_Earth_v1/".
VARIANTS = {"deterministic": "deterministic_raw_daily", "probabilistic": "probabilistic_daily"}
SOURCES = ("s3", "frdr")
# FRDR's documented stable per-file link (302s to the Globus HTTPS collection).
FRDR_BASE = (
    "https://www.frdr-dfdr.ca/repo/files/6/published/publication_542/"
    "submitted_data/EM_Earth_v1"
)

# tmean/tdew are °C → +273.15 (QC-protected). prcp is mm/day → /86400, verified
# against the FRDR file attrs (units: 'mm day-1', 2026-06-12).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tmean", CanonicalVar.AIR_TEMPERATURE, offset=273.15),
    VariableMapping("tdew", CanonicalVar.DEWPOINT_TEMPERATURE, offset=273.15),
    VariableMapping(
        "prcp", CanonicalVar.PRECIPITATION_FLUX, scale=1.0 / 86400.0,
        note="EM-Earth daily prcp mm/day -> flux (verified vs FRDR file attrs)",
    ),
]
# canonical → source var the mapping reads (for request-driven file selection).
_SRC = {m.canonical: m.source_name for m in _MAPPINGS}


@register("em_earth")
class EMEarthConnector(BaseForcingConnector):
    slug = "em_earth"
    display_name = "EM-Earth (0.1° daily, global; S3 credential-gated / FRDR HTTPS)"
    base_url = f"s3://{BUCKET}"
    protocol = "s3_direct"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = self.config or {}
        self.anon = cfg.get("anon", True)
        self.variant = cfg.get("variant", "deterministic")
        if self.variant not in VARIANTS:
            raise SubsetError(f"Unknown EM-Earth variant '{self.variant}' (deterministic/probabilistic)")
        self.source = cfg.get("source", "frdr" if self.variant == "deterministic" else "s3")
        if self.source not in SOURCES:
            raise SubsetError(f"Unknown EM-Earth source '{self.source}' (s3/frdr)")
        if self.source == "frdr" and self.variant != "deterministic":
            raise SubsetError(
                "EM-Earth source 'frdr' supports only the deterministic variant: "
                "FRDR's probabilistic archive is split by continent/member and "
                "does not mirror the S3 key layout. Stage probabilistic files "
                "via Globus and point data_dir at them instead."
            )
        data_dir = cfg.get("data_dir")
        self.data_dir = Path(data_dir) if data_dir else None
        self._fs = None
        self._tmp_cache: Path | None = None

    def _filesystem(self):
        if self._fs is None:
            try:
                import s3fs
            except ImportError as e:  # pragma: no cover
                raise MissingExtraError("EM-Earth needs s3fs (the 'climate' extra).") from e
            self._fs = s3fs.S3FileSystem(anon=self.anon)
        return self._fs

    async def list_products(self) -> list[ForcingProduct]:
        from datetime import datetime

        return [
            ForcingProduct(
                id=f"{self.slug}:{self.variant}_daily",
                provider=self.slug,
                name=f"EM-Earth {self.variant} daily (0.1°, global)",
                description=(
                    f"EM-Earth {self.variant} daily met (temperature, dewpoint, "
                    "precipitation). The deterministic product defaults to the "
                    "anonymous FRDR per-file HTTPS archive; S3 remains available "
                    "with source='s3' and credentials."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(
                    start=datetime(1950, 1, 1), end=datetime(2019, 12, 31),
                    resolution=TemporalResolution.DAILY,
                ),
                protocol=Protocol.S3_DIRECT,
                license="EM-Earth (cite Tang et al. 2022); bucket access may be restricted.",
                citation="Tang et al. (2022), EM-Earth, BAMS 103:E996-E1018.",
            )
        ]

    def _fname(self, var: str, year: int, month: int) -> str:
        return f"EM_Earth_{self.variant}_daily_{var}_{year}{month:02d}.nc"

    def _key(self, var: str, year: int, month: int) -> str:
        folder = VARIANTS[self.variant]
        return f"{BUCKET}/nc/{folder}/{var}/{self._fname(var, year, month)}"

    def _frdr_url(self, var: str, year: int, month: int) -> str:
        folder = VARIANTS[self.variant]
        return f"{FRDR_BASE}/{folder}/{var}/{self._fname(var, year, month)}"

    def _staged_path(self, var: str, year: int, month: int) -> Path | None:
        """Locate a pre-staged monthly file under ``data_dir`` (if any).

        Looks for the archive-relative layout ``<variant_folder>/<var>/<fname>``
        first, then a flat ``<fname>`` directly under ``data_dir``.
        """
        if self.data_dir is None:
            return None
        fname = self._fname(var, year, month)
        for cand in (self.data_dir / VARIANTS[self.variant] / var / fname,
                     self.data_dir / fname):
            if cand.is_file():
                return cand
        return None

    def _frdr_download(self, var: str, year: int, month: int) -> Path | None:
        """Download one monthly file from FRDR's anonymous per-file HTTPS route.

        Streams into ``data_dir`` (archive-relative layout) when configured,
        otherwise into a per-connector temp cache. Returns None on 404 (month
        outside the archive); raises ``SubsetError`` on other HTTP failures.
        """
        import tempfile

        import requests

        url = self._frdr_url(var, year, month)
        if self.data_dir is not None:
            dest = self.data_dir / VARIANTS[self.variant] / var / self._fname(var, year, month)
        else:
            if self._tmp_cache is None:
                self._tmp_cache = Path(tempfile.mkdtemp(prefix="cfs_em_earth_"))
                logger.warning(
                    "em_earth: no data_dir configured — FRDR downloads "
                    "(~100-300 MB per variable-month) go to a temp cache and "
                    "are not reused across sessions",
                    tmp_cache=str(self._tmp_cache),
                )
            dest = self._tmp_cache / self._fname(var, year, month)
        if dest.is_file():
            return dest

        logger.info("em_earth: downloading from FRDR", url=url, dest=str(dest))
        resp = requests.get(url, stream=True, timeout=600, allow_redirects=True)
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise RegistrationRequiredError(
                self.slug,
                "https://www.frdr-dfdr.ca/repo/dataset/8d30ab02-f2bd-4d05-ae43-11f4a387e5ad",
                f"FRDR denied access to {url} (HTTP {resp.status_code}). The "
                "per-file HTTPS route was anonymous when last verified "
                "(2026-06-12); if it is gated now, stage files via Globus and "
                "set config={'data_dir': ...}.",
            )
        try:
            resp.raise_for_status()
        except Exception as e:
            raise SubsetError(f"FRDR download failed for {url}: {e}") from e
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
        return dest

    def _open_month(self, var: str, year: int, month: int) -> xr.Dataset | None:
        """Open one variable-month as a lazy dataset, or None if unavailable.

        Order: pre-staged ``data_dir`` file (no network), then the configured
        source (S3 needs bucket credentials; FRDR is anonymous HTTPS).
        """
        import xarray as xr

        staged = self._staged_path(var, year, month)
        if staged is not None:
            return xr.open_dataset(staged, engine="h5netcdf")

        if self.source == "frdr":
            path = self._frdr_download(var, year, month)
            if path is None:
                return None
            return xr.open_dataset(path, engine="h5netcdf")

        fs = self._filesystem()
        key = self._key(var, year, month)
        try:
            raw = fs.cat_file(key)
        except PermissionError as e:
            raise RegistrationRequiredError(
                self.slug,
                "https://www.frdr-dfdr.ca/repo/dataset/8d30ab02-f2bd-4d05-ae43-11f4a387e5ad",
                "The 'emearth' S3 bucket denied access. It is no longer anonymously "
                "readable — provide AWS credentials with bucket access (config={'anon': "
                "False} + standard AWS credential chain), use the anonymous FRDR HTTPS "
                "route (config={'source': 'frdr'}), or stage FRDR/Globus downloads "
                "locally (config={'data_dir': ...}).",
            ) from e
        except (FileNotFoundError, OSError):
            return None
        return xr.open_dataset(io.BytesIO(raw), engine="h5netcdf")

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import pandas as pd
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        wanted = set(variables) if variables else {m.canonical for m in _MAPPINGS}
        selected = [m for m in _MAPPINGS if m.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by EM-Earth")
        src_vars = [m.source_name for m in selected]

        warnings: list[str] = []

        # (year, month) chunks over the request, clamped to the 1950-2019 record.
        months = pd.date_range(
            pd.Timestamp(time_range.start).to_period("M").to_timestamp(),
            pd.Timestamp(time_range.end).to_period("M").to_timestamp(),
            freq="MS",
        )
        per_var_concat = {}
        for var in src_vars:
            chunks = []
            for ms in months:
                if not (1950 <= ms.year <= 2019):
                    continue
                ds_month = self._open_month(var, ms.year, ms.month)
                if ds_month is None:
                    continue
                ds = ds_month[[var]]
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
                ds = ds.sel(time=slice(time_range.start, time_range.end))
                if ds.sizes.get("time", 0) > 0:
                    chunks.append(ds.load())
                ds_month.close()
            if chunks:
                per_var_concat[var] = xr.concat(chunks, dim="time")

        if not per_var_concat:
            raise SubsetError(f"No EM-Earth data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.merge(list(per_var_concat.values()), join="inner")

        canonical = harmonize(ds_all, selected, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"EM-Earth {self.variant} via {self.source}; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings,
        )
