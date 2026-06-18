# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""MRMS connector — NOAA Multi-Radar/Multi-Sensor QPE precipitation (AWS GRIB2).

Real-time, high-resolution (~1 km, 2-minute cadence) radar+gauge quantitative
precipitation estimates for CONUS, read anonymously from the public
``noaa-mrms-pds`` S3 archive. This is the strongest **nowcasting** precip source
in CFS.

**Precipitation only.** MRMS QPE carries no temperature, humidity, pressure,
wind, or radiation — it *cannot* supply the eight-variable forcing set on its
own. Pair it with a full-forcing source (HRRR/GFS/AORC/NLDAS) when a complete
forcing cube is needed; use MRMS where the best-available observed precipitation
matters.

This connector exposes the gauge-corrected **MultiSensor QPE 1-hour Pass-2**
product (``MultiSensor_QPE_01H_Pass2``): radar QPE bias-corrected against hourly
gauges, the most complete of the latency tiers. Each timestamp is a separate
gzip-compressed GRIB2 object
(``CONUS/<product>/<YYYYMMDD>/MRMS_<product>_<YYYYMMDD>-<HHMMSS>.grib2.gz``); the
01H product publishes one file per hour. The connector lists the S3 prefix for
each requested day, picks the file nearest each valid hour, downloads it whole,
**gunzips** it, and decodes the single GRIB message with cfgrib (the file holds
one field). The native grid is a regular 0.01° lat/lon on 0–360 longitude
(normalized on subset); the QPE accumulation (mm over the hour) becomes a mass
flux ``kg m-2 s-1`` by dividing by the 3600 s window. MRMS no-coverage / missing
sentinels (negative fills) are masked to NaN.

The S3 archive is a rolling window (≈5.6 yr; earliest ≈2020-10-14), so requests
must fall inside the retained range. Needs the ``cfgrib``/``eccodes`` stack (the
``forecast`` extra). Anonymous, so this connector is live-verifiable.
"""

from __future__ import annotations

import gzip
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import http_range, open_message
from cfs.core.config import get_settings
from cfs.core.exceptions import MissingExtraError, SubsetError
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

S3_BASE = "https://noaa-mrms-pds.s3.amazonaws.com"
_PRODUCT_TOKEN = "MultiSensor_QPE_01H_Pass2_00.00"
_ACCUM_SECONDS = 3600  # 1-hour accumulation window
# Match each requested valid hour to the file at most this far away (the 01H
# product is hourly; tolerance guards against a missing/late timestamp).
_MATCH_TOLERANCE_S = 1800

# MRMS QPE GRIB carries a single (unnamed) precipitation field. open_message
# renames it to this internal placeholder; the accumulation (mm over the window)
# becomes a mass flux by dividing by the window length.
_INTERNAL = "_qpe"
_MAPPINGS: list[VariableMapping] = [
    VariableMapping(
        _INTERNAL,
        CanonicalVar.PRECIPITATION_FLUX,
        scale=1.0 / _ACCUM_SECONDS,
        note="MRMS 1 h QPE (mm) -> mass flux (kg m-2 s-1); 1 mm = 1 kg m-2 over 3600 s",
    ),
]


def _prefix(day: datetime) -> str:
    return f"CONUS/{_PRODUCT_TOKEN}/{day:%Y%m%d}/"


def _object_url(key: str) -> str:
    return f"{S3_BASE}/{key}"


def _parse_key_time(key: str) -> datetime | None:
    """Extract the timestamp from an MRMS object key, or None if it doesn't match.

    Filenames end ``..._YYYYMMDD-HHMMSS.grib2.gz``.
    """
    name = key.rsplit("/", 1)[-1]
    stamp = name.removeprefix(f"MRMS_{_PRODUCT_TOKEN}_").removesuffix(".grib2.gz")
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _nearest_key(
    keyed: list[tuple[datetime, str]], target: datetime, tolerance_s: int = _MATCH_TOLERANCE_S
) -> str | None:
    """Pick the object key whose timestamp is nearest ``target`` within tolerance.

    ``keyed`` is ``[(timestamp, key)]``. Returns None if the closest file is
    further than ``tolerance_s`` from ``target`` (treated as "no data here").
    """
    best: tuple[float, str] | None = None
    for ts, key in keyed:
        delta = abs((ts - target).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, key)
    if best is None or best[0] > tolerance_s:
        return None
    return best[1]


def _s3_list(prefix: str) -> list[str]:
    """List every object key under ``prefix`` (anonymous S3 REST, paginated)."""
    keys: list[str] = []
    token: str | None = None
    timeout = get_settings().provider_timeout_s
    while True:
        url = f"{S3_BASE}/?list-type=2&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
        if token:
            url += f"&continuation-token={urllib.parse.quote(token)}"
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - https S3
            root = ET.fromstring(r.read())
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        keys += [e.text for e in root.findall(".//s3:Contents/s3:Key", ns) if e.text]
        truncated = root.findtext("s3:IsTruncated", default="false", namespaces=ns)
        token = root.findtext("s3:NextContinuationToken", default=None, namespaces=ns)
        if truncated != "true" or not token:
            break
    return keys


@register("mrms")
class MRMSConnector(BaseForcingConnector):
    slug = "mrms"
    display_name = "NOAA MRMS MultiSensor QPE (1 km, hourly, CONUS; precip only)"
    base_url = S3_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:multisensor_qpe_01h",
                provider=self.slug,
                name="MRMS MultiSensor QPE 1-hour Pass-2 (1 km CONUS)",
                description=(
                    "NOAA Multi-Radar/Multi-Sensor gauge-corrected 1-hour quantitative "
                    "precipitation estimate (MultiSensor_QPE_01H_Pass2), downloaded per "
                    "timestamp from the noaa-mrms-pds S3 archive, gunzipped and decoded "
                    "with cfgrib. PRECIPITATION ONLY — pair with a full-forcing source. "
                    "S3 retention is a rolling ~5.6 yr window."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.01,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-130.0, min_lat=20.0, max_lon=-60.0, max_lat=55.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA/NSSL Multi-Radar/Multi-Sensor (MRMS) QPE.",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "MRMS GRIB2 decoding needs the 'forecast' extra: "
                "pip install -e '.[forecast]' (cfgrib + eccodes)"
            ) from e

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
        self._require_cfgrib()

        if variables and CanonicalVar.PRECIPITATION_FLUX not in variables:
            raise SubsetError(
                "MRMS offers only precipitation_flux (it is a precipitation-only QPE)"
            )

        # One S3 listing per UTC day spanning the request → {timestamp: key}.
        days = pd.date_range(time_range.start.date(), time_range.end.date(), freq="D")
        keyed: list[tuple[datetime, str]] = []
        for day in days:
            for key in await self._to_thread(_s3_list, _prefix(day.to_pydatetime())):
                ts = _parse_key_time(key)
                if ts is not None:
                    keyed.append((ts, key))
        if not keyed:
            raise SubsetError(
                f"No MRMS QPE objects in [{time_range.start}, {time_range.end}] "
                f"(S3 retains a rolling ~5.6 yr window; earliest ~2020-10-14)"
            )

        valid_times = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            key = _nearest_key(keyed, valid.to_pydatetime())
            if key is None:
                warnings.append(f"No MRMS QPE within {_MATCH_TOLERANCE_S}s of {valid:%Y-%m-%dT%H}")
                return None
            raw = gzip.decompress(http_range(_object_url(key), 0, ""))
            ds = open_message(raw, _INTERNAL, label="MRMS")
            # Mask MRMS no-coverage / missing sentinels (negative fills).
            ds = ds.where(ds[_INTERNAL] >= 0)
            plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
            ds = apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude")
            return ds.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No MRMS QPE matched the valid times in [{time_range.start}, {time_range.end}]"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                "NOAA MRMS MultiSensor_QPE_01H_Pass2 via noaa-mrms-pds S3 "
                "(gunzip + cfgrib); canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )

    @staticmethod
    async def _to_thread(fn, *args):
        import asyncio

        return await asyncio.to_thread(fn, *args)
