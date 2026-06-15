# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""GFS connector — NOAA Global Forecast System atmospheric forecast (AWS GRIB2).

The first *forecast* forcing connector: global 0.25° atmospheric forecasts from
the NOAA GFS, read from the public ``noaa-gfs-bdp-pds`` S3 archive (anonymous).

GFS files are GRIB2 (~500 MB each, all variables/levels). Downloading them whole
to pull a handful of surface fields would be wasteful, so this connector uses the
``.idx`` sidecar (a byte-offset index of every GRIB message) to HTTP **byte-range**
fetch only the messages it needs — the "Herbie" pattern — then decodes each with
cfgrib. A typical surface-forcing fetch transfers a few MB per lead hour instead
of gigabytes.

Forecast model. GFS runs four cycles a day (00/06/12/18 UTC), each producing lead
hours f000 (analysis) … f384. For a requested time range, CFS picks the most
recent cycle at or before the range start and pulls the lead hours whose *valid*
time (cycle + lead) falls in the window — i.e. "the forecast initialised at or
just before the requested start". Lead hours are 1-hourly to f120 and 3-hourly to
f384; valid times that need an unavailable lead are skipped (warned). The archive
retains a long history, but very old cycles may age off.

All exposed fields are already canonical SI (TMP K, SPFH kg/kg, PRES Pa, UGRD/VGRD
m/s, PRATE kg m⁻² s⁻¹, DSWRF/DLWRF W m⁻²), so every mapping is identity. Radiation
and precip are forecast-interval averages and are absent from f000 (the analysis),
so they appear only from lead ≥ 1. Wind is returned as u/v components. The grid is
regular lat/lon on a 0–360 longitude (requested lons are normalized).

Needs the ``cfgrib``/``eccodes`` stack (the ``forecast`` extra). Anonymous, so
this connector is live-verifiable.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import cycle_for, http_range, parse_idx, read_field
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
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"


class _GFSVar:
    """A GFS field: GRIB (variable, level) in the .idx → canonical, identity SI."""

    __slots__ = ("grib_var", "grib_level", "canonical", "internal")

    def __init__(self, grib_var: str, grib_level: str, canonical: CanonicalVar, internal: str):
        self.grib_var = grib_var
        self.grib_level = grib_level
        self.canonical = canonical
        self.internal = internal


# Surface forcing fields (the .idx level strings are matched exactly).
_VARS: list[_GFSVar] = [
    _GFSVar("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE, "_t2m"),
    _GFSVar("SPFH", "2 m above ground", CanonicalVar.SPECIFIC_HUMIDITY, "_q2m"),
    _GFSVar("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp"),
    _GFSVar("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND, "_u10"),
    _GFSVar("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND, "_v10"),
    _GFSVar("PRATE", "surface", CanonicalVar.PRECIPITATION_FLUX, "_prate"),
    _GFSVar("DSWRF", "surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "_dswrf"),
    _GFSVar("DLWRF", "surface", CanonicalVar.LONGWAVE_RADIATION_DOWN, "_dlwrf"),
]

_MAPPINGS: list[VariableMapping] = [VariableMapping(v.internal, v.canonical) for v in _VARS]

# GFS pgrb2.0p25 lead hours: 1-hourly to f120, then 3-hourly to f384.
_MAX_HOURLY_LEAD = 120
_MAX_LEAD = 384


def _lead_available(lead: int) -> bool:
    if lead < 0 or lead > _MAX_LEAD:
        return False
    return lead <= _MAX_HOURLY_LEAD or lead % 3 == 0


def _file_url(cycle: datetime, lead: int) -> str:
    d = cycle.strftime("%Y%m%d")
    h = cycle.strftime("%H")
    return f"{S3_BASE}/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{lead:03d}"


@register("gfs")
class GFSConnector(BaseForcingConnector):
    slug = "gfs"
    display_name = "NOAA GFS atmospheric forecast (0.25°, hourly, global)"
    base_url = S3_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:forecast_0p25",
                provider=self.slug,
                name="GFS 0.25° atmospheric forecast (global)",
                description=(
                    "NOAA Global Forecast System surface forcing, byte-range read "
                    "from the GRIB2 pgrb2.0p25 files on the noaa-gfs-bdp-pds S3 "
                    "archive. The forecast initialised at/before the requested start "
                    "supplies the valid-time forcing."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.25,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP Global Forecast System (GFS).",
            )
        ]

    @staticmethod
    def _require_cfgrib():
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "GFS GRIB2 decoding needs the 'forecast' extra: "
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

        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by GFS")

        cycle = cycle_for(time_range.start)
        valid_times = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not _lead_available(lead):
                warnings.append(
                    f"GFS lead f{lead:03d} (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz}"
                )
                return None
            url = _file_url(cycle, lead)
            idx_recs = parse_idx(http_range(url + ".idx", 0, "").decode())
            # radiation/precip are absent at f000 (analysis): read_field → None, skipped.
            per_var = [
                f for v in selected
                if (f := read_field(url, idx_recs, v.grib_var, v.grib_level, v.internal, bbox, label="GFS"))
                is not None
            ]
            if not per_var:
                return None
            merged = xr.merge(per_var, join="inner")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces(
            [partial(_piece, v) for v in valid_times], concurrency=1
        )
        if not pieces:
            raise SubsetError(
                f"No GFS data in [{time_range.start}, {time_range.end}] from cycle "
                f"{cycle:%Y%m%d %Hz} (archive retains recent cycles; old ones age off)"
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
                f"GFS pgrb2.0p25 via noaa-gfs-bdp-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
