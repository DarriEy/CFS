# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NOAA CPC CMORPH CDR daily precipitation connector.

Reads the CPC daily ``.tar`` archive (one tar per month, each holding daily
0.25° NetCDFs) over HTTPS, extracts the requested days, and subsets to the bbox
(the grid is on a 0–360 longitude that :mod:`cfs.subset.bbox` normalizes).
``cmorph`` is a daily total in mm/day → ``precipitation_flux`` via ``/86400``.

Note the CPC endpoint only hosts a **rolling recent window** (roughly the last
couple of months of daily tars); historical years are not served here, so a fetch
outside the listed window raises a clear error. Live-verified for an available
recent month.
"""

from __future__ import annotations

import calendar
import re
import tarfile
import time
from pathlib import Path

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.http_files import HTTPFilesMixin
from cfs.core.config import get_settings
from cfs.core.exceptions import ConnectorError, SubsetError
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

logger = structlog.get_logger()

CPC_ROOT = "https://ftp.cpc.ncep.noaa.gov/precip/CDR_CMORPH"
_DAILY_RE = re.compile(r'href="(?P<name>cmorph_v1\.0_0\.25deg_daily_s(?P<start>\d{8})_e(?P<end>\d{8})_c\d{8}\.tar)"')

_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        "cmorph",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / 86400.0,
        note="CMORPH daily precipitation (mm/day) -> flux (kg m-2 s-1)",
    )
]


def _months(time_range: TimeRange) -> list[tuple[int, int]]:
    y, m = time_range.start.year, time_range.start.month
    end_y, end_m = time_range.end.year, time_range.end.month
    out: list[tuple[int, int]] = []
    while (y, m) <= (end_y, end_m):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _parse_daily_tars(index_html: str) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    for m in _DAILY_RE.finditer(index_html):
        start = m.group("start")
        key = (int(start[:4]), int(start[4:6]))
        out[key] = m.group("name")
    return out


def _member_name(year: int, month: int, day: int) -> str:
    return f"CMORPH_V1.0_ADJ_0.25deg-DLY_00Z_{year}{month:02d}{day:02d}.nc"


def _days_in_month(year: int, month: int, time_range: TimeRange) -> list[int]:
    first = max(1, time_range.start.day if (year, month) == (time_range.start.year, time_range.start.month) else 1)
    last_day = calendar.monthrange(year, month)[1]
    if (year, month) == (time_range.end.year, time_range.end.month):
        last = min(last_day, time_range.end.day)
    else:
        last = last_day
    return list(range(first, last + 1))


@register("cmorph")
class CMORPHConnector(HTTPFilesMixin, BaseForcingConnector):
    slug = "cmorph"
    display_name = "NOAA CPC CMORPH CDR daily precipitation (0.25°)"
    base_url = CPC_ROOT
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="CMORPH CDR daily precipitation (0.25°)",
                description=(
                    "NOAA CPC Morphing Technique Climate Data Record daily "
                    "bias-corrected precipitation, distributed as monthly tar files."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-60, max_lon=180, max_lat=60),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.REST,
                license="NOAA public data.",
                citation="Xie et al. (2017), Reprocessed bias-corrected CMORPH CDR.",
            )
        ]

    def _index(self) -> str:
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise ConnectorError(self.slug, "CMORPH HTTP index access needs requests installed") from e
        r = requests.get(f"{CPC_ROOT}/", timeout=get_settings().provider_timeout_s)
        if r.status_code != 200:
            raise ConnectorError(self.slug, f"HTTP {r.status_code} for {CPC_ROOT}/")
        return r.text

    def _extract_member(self, tar_path: Path, member: str) -> Path:
        dest = self._cache_dir() / member
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        with tarfile.open(tar_path) as tf:
            try:
                src = tf.extractfile(member)
            except KeyError as e:
                raise SubsetError(f"CMORPH tar {tar_path.name} lacks {member}") from e
            if src is None:
                raise SubsetError(f"CMORPH tar member {member} is not a file")
            with open(dest, "wb") as fh:
                fh.write(src.read())
        return dest

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[object, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        if variables is not None and CanonicalVar.PRECIPITATION_FLUX not in set(variables):
            raise SubsetError("CMORPH offers only precipitation_flux")

        available = _parse_daily_tars(self._index())
        pieces = []
        for year, month in _months(time_range):
            tar_name = available.get((year, month))
            if not tar_name:
                raise SubsetError(
                    f"No CMORPH daily tar listed for {year}-{month:02d}; the CPC endpoint "
                    f"only hosts a rolling recent window ({len(available)} month(s) available)."
                )
            tar_path = self._download_cached(f"{CPC_ROOT}/{tar_name}", tar_name)
            for day in _days_in_month(year, month, time_range):
                nc_path = self._extract_member(tar_path, _member_name(year, month, day))
                ds = xr.open_dataset(nc_path)[["cmorph"]]
                plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
                ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
                pieces.append(ds)

        if not pieces:
            raise SubsetError(f"No CMORPH data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        ds_all = ds_all.sel(time=slice(time_range.start, time_range.end))
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No CMORPH data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="NOAA CPC CMORPH CDR daily tar archive; local day extraction and bbox subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
