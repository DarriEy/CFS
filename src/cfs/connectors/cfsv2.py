# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CFSv2 connector — NCEP Climate Forecast System v2 / CDAS analysis (AWS GRIB2).

The real-time global analysis backbone that extends CFSR, read from the public
``noaa-cfs-pds`` S3 archive (anonymous) with the shared Herbie ``.idx``
byte-range + cfgrib machinery (:mod:`cfs.connectors.protocols.grib_idx`) — the
same pattern as ``gfs``/``gefs``.

**What this is (and isn't).** The ``noaa-cfs-pds`` bucket is the CDAS (Climate
Data Assimilation System) real-time analysis + short-range stream — files
``cdas.YYYYMMDD/cdas1.tHHz.sfluxgrbf{00..09}.grib2`` at the 00/06/12/18Z cycles,
each with a 0–9 h lead. It is the operational analysis/nowcast that extends
CFSR, **not** the 9-month coupled *seasonal* forecast (that lives on NOMADS, a
7-day rolling window, and in the NCAR RDA ``ds094.0`` archive). So this connector
is a global ~0.5° near-real-time forcing source, modelled like ``gfs``: the most
recent 00/06/12/18Z cycle at/before the requested start supplies each valid hour
(``lead = valid − cycle``), with leads available f00…f09 from that cycle.

The surface-flux file (``sfluxgrbf``) carries all eight forcing fields under the
NCEP/GFS-family GRIB2 short names (``TMP``/``SPFH``/``PRES`` 2 m/surface,
``UGRD``/``VGRD`` 10 m, ``PRATE``/``DSWRF``/``DLWRF`` surface), so every mapping
is identity SI and the byte-range/decode plumbing is shared with ``gfs``. The
instantaneous state fields are analysis (``anl``) records present at every lead;
the flux fields (``PRATE``/``DSWRF``/``DLWRF``) are forecast-interval averages
and are absent from f00 (the pure analysis), so they appear only from lead ≥ 1 —
exactly as in ``gfs``. Wind is returned as u/v components. The native grid is a
global ~0.5° Gaussian lat/lon on 0–360 longitude (requested lons normalized).

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

S3_BASE = "https://noaa-cfs-pds.s3.amazonaws.com"


class _CFSVar:
    """A CFSv2 sfluxgrbf field: GRIB (variable, level) in the .idx → canonical, identity SI."""

    __slots__ = ("grib_var", "grib_level", "canonical", "internal")

    def __init__(self, grib_var: str, grib_level: str, canonical: CanonicalVar, internal: str):
        self.grib_var = grib_var
        self.grib_level = grib_level
        self.canonical = canonical
        self.internal = internal


# Surface forcing fields in the sfluxgrbf .idx (level strings matched exactly).
# Instantaneous state fields carry "anl"; PRATE/DSWRF/DLWRF are interval
# averages absent at f00 (read_field → None, skipped), like gfs.
_VARS: list[_CFSVar] = [
    _CFSVar("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE, "_t2m"),
    _CFSVar("SPFH", "2 m above ground", CanonicalVar.SPECIFIC_HUMIDITY, "_q2m"),
    _CFSVar("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE, "_sp"),
    _CFSVar("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND, "_u10"),
    _CFSVar("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND, "_v10"),
    _CFSVar("PRATE", "surface", CanonicalVar.PRECIPITATION_FLUX, "_prate"),
    _CFSVar("DSWRF", "surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "_dswrf"),
    _CFSVar("DLWRF", "surface", CanonicalVar.LONGWAVE_RADIATION_DOWN, "_dlwrf"),
]

_MAPPINGS: list[VariableMapping] = [VariableMapping(v.internal, v.canonical) for v in _VARS]

# CDAS sfluxgrbf leads: f00 (analysis) … f09, hourly.
_MAX_LEAD = 9


def _lead_available(lead: int) -> bool:
    return 0 <= lead <= _MAX_LEAD


def _file_url(cycle: datetime, lead: int) -> str:
    d = cycle.strftime("%Y%m%d")
    h = cycle.strftime("%H")
    return f"{S3_BASE}/cdas.{d}/cdas1.t{h}z.sfluxgrbf{lead:02d}.grib2"


@register("cfsv2")
class CFSv2Connector(BaseForcingConnector):
    slug = "cfsv2"
    display_name = "NCEP CFSv2/CDAS analysis (~0.5°, hourly, global)"
    base_url = S3_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:cdas_flux",
                provider=self.slug,
                name="CFSv2/CDAS surface-flux analysis (~0.5° global)",
                description=(
                    "NCEP Climate Forecast System v2 / CDAS surface forcing, "
                    "byte-range read from the sfluxgrbf GRIB2 files on the "
                    "noaa-cfs-pds S3 archive. The 00/06/12/18Z cycle at/before the "
                    "requested start supplies the valid-time forcing (leads f00–f09); "
                    "this is the real-time analysis stream that extends CFSR, not the "
                    "9-month seasonal forecast (NOMADS/RDA)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.5,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation=(
                    "Saha et al. (2014), The NCEP Climate Forecast System Version 2, "
                    "J. Climate 27:2185-2208."
                ),
            )
        ]

    @staticmethod
    def _require_cfgrib():
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "CFSv2 GRIB2 decoding needs the 'forecast' extra: "
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
            raise SubsetError("None of the requested variables are offered by CFSv2")

        cycle = cycle_for(time_range.start, step_h=6)
        valid_times = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not _lead_available(lead):
                warnings.append(
                    f"CFSv2 lead f{lead:02d} (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz} (CDAS sfluxgrbf is f00–f09)"
                )
                return None
            url = _file_url(cycle, lead)
            idx_recs = parse_idx(http_range(url + ".idx", 0, "").decode())
            # PRATE/DSWRF/DLWRF are absent at f00 (analysis): read_field → None, skipped.
            per_var = [
                f for v in selected
                if (f := read_field(url, idx_recs, v.grib_var, v.grib_level, v.internal, bbox,
                                    label="CFSv2"))
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
                f"No CFSv2 data in [{time_range.start}, {time_range.end}] from cycle "
                f"{cycle:%Y%m%d %Hz} (bucket history begins 2023-04-22; old cycles age off)"
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
                f"CFSv2/CDAS sfluxgrbf via noaa-cfs-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
        )
