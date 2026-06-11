# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""PERSIANN-CDR connector — NOAA/NCEI global daily satellite precipitation CDR.

PERSIANN-CDR (Precipitation Estimation from Remotely Sensed Information using
Artificial Neural Networks — Climate Data Record) is a quasi-global (60°S–60°N)
0.25° daily satellite precipitation Climate Data Record running 1983-01-01 to
present. NCEI publishes one plain-HTTP NetCDF-4/HDF5 file per day under
``access/{YYYY}/PERSIANN-CDR_v01r01_{YYYYMMDD}_c{CCCCCCCC}.nc`` — the trailing
``_c{creation-date}`` token is NOT predictable, so this connector reads each
year's HTML directory index to resolve the exact filename for a requested day.

Each daily file is a small (~1.2 MB) chunked HDF5 on a range-capable server, so
it is opened **lazily over HTTP byte-range** (the bbox subset transfers only the
overlapping chunks). The ``precipitation`` field (mm/day, dimensioned
``(time, lon, lat)`` on a 0–360 longitude grid) is harmonized to the canonical
``precipitation_flux`` (kg m-2 s-1) via ``/86400`` and re-oriented to
``(time, latitude, longitude)``. A precip-only product → one canonical variable.

Caveats: the record starts 1983-01-01; the most recent days lag real time (NCEI
posts PERSIANN-CDR with a multi-week latency). A requested day with no published
file (future, or outside the served range) raises a clear error.
"""

from __future__ import annotations

import re
import time
from functools import partial
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

NCEI_ROOT = "https://www.ncei.noaa.gov/data/precipitation-persiann/access"
SECONDS_PER_DAY = 86400.0

# access/{YYYY}/PERSIANN-CDR_v01r01_{YYYYMMDD}_c{YYYYMMDD}.nc — the creation-date
# token varies per file, so the day's filename is looked up in the year index.
_FILE_RE = re.compile(
    r'href="(?P<name>PERSIANN-CDR_v01r01_(?P<ymd>\d{8})_c\d{8}\.nc)"'
)

# PERSIANN-CDR daily precip is mm/day → mass flux: /86400 (1 mm ≡ 1 kg m-2).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        "precipitation",
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / SECONDS_PER_DAY,
        note="PERSIANN-CDR daily total (mm/day) -> flux (kg m-2 s-1)",
    ),
]


def _days(time_range: TimeRange) -> list[str]:
    """Return the YYYYMMDD strings spanned by ``time_range`` (inclusive)."""
    import pandas as pd

    rng = pd.date_range(time_range.start.date(), time_range.end.date(), freq="D")
    return [d.strftime("%Y%m%d") for d in rng]


def _parse_year_index(index_html: str) -> dict[str, str]:
    """Map ``YYYYMMDD`` -> filename for every daily file in a year's index."""
    return {m.group("ymd"): m.group("name") for m in _FILE_RE.finditer(index_html)}


@register("persiann_cdr")
class PersiannCDRConnector(HTTPFilesMixin, BaseForcingConnector):
    slug = "persiann_cdr"
    display_name = "PERSIANN-CDR daily precipitation (0.25°)"
    base_url = NCEI_ROOT
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="PERSIANN-CDR daily precipitation (0.25°)",
                description=(
                    "NOAA/NCEI PERSIANN Climate Data Record, quasi-global "
                    "(60°S–60°N) 0.25° daily precipitation, one HDF5 file per day "
                    "from 1983-01-01."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180.0, min_lat=-60.0, max_lon=180.0, max_lat=60.0),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.REST,
                license="NOAA/NCEI public data (open).",
                citation="Ashouri et al. (2015), PERSIANN-CDR, Bull. Amer. Meteor. Soc. 96:69-83.",
            )
        ]

    def _year_index(self, year: int) -> str:
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise ConnectorError(self.slug, "PERSIANN-CDR HTTP index access needs requests installed") from e
        url = f"{NCEI_ROOT}/{year}/"
        r = requests.get(url, timeout=get_settings().provider_timeout_s)
        if r.status_code != 200:
            raise ConnectorError(self.slug, f"HTTP {r.status_code} for {url}")
        return r.text

    async def fetch(
        self,
        product_id: str,
        bbox: BoundingBox,
        time_range: TimeRange,
        variables: list[CanonicalVar] | None = None,
    ) -> tuple[xr.Dataset, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)

        if variables is not None and CanonicalVar.PRECIPITATION_FLUX not in set(variables):
            raise SubsetError("PERSIANN-CDR offers only precipitation_flux")

        ymds = _days(time_range)
        years = sorted({int(d[:4]) for d in ymds})
        index: dict[str, str] = {}
        for year in years:
            index.update(_parse_year_index(self._year_index(year)))

        urls: list[tuple[str, str]] = []
        for ymd in ymds:
            name = index.get(ymd)
            if not name:
                raise SubsetError(
                    f"No PERSIANN-CDR file published for {ymd}; the record runs "
                    "1983-01-01 onward with a multi-week latency for recent days."
                )
            urls.append((ymd, f"{NCEI_ROOT}/{ymd[:4]}/{name}"))

        def _piece(url: str):
            ds = self._open_http_lazy(url)[["precipitation"]]
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            # Materialize the (small) overlapping chunks pulled over byte-range.
            return ds.load()

        pieces = await self._gather_pieces([partial(_piece, url) for _, url in urls])

        if not pieces:
            raise SubsetError(
                f"No PERSIANN-CDR data in [{time_range.start}, {time_range.end}] for the bbox"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        # PERSIANN files are dimensioned (time, lon, lat) — normalize orientation.
        canonical = canonical.transpose("time", "latitude", "longitude", ...)
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="NOAA/NCEI PERSIANN-CDR daily HDF5; HTTP byte-range subset; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
        )
