# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""RAP connector — NOAA Rapid Refresh atmospheric forecast (AWS GRIB2).

The hourly-updating, 13 km CONUS companion to :mod:`cfs.connectors.hrrr`'s
forecast stream: surface forcing from the NOAA Rapid Refresh, read from the
public ``noaa-rap-pds`` S3 archive (anonymous) with the same Herbie ``.idx``
byte-range + cfgrib machinery (shared :mod:`cfs.connectors.protocols.grib_idx`).

Reads the ``awp130`` product (Lambert Conformal grid 130, ~13 km CONUS). The
forecast model matches ``gfs``/``hrrr``: the most recent hourly cycle at or
before the requested start supplies each valid hour (``lead = valid − cycle``).
RAP runs every hour; standard cycles go to f21, and the 03/09/15/21Z runs
extend to f51 (live-probed 2026-06-13).

Every exposed field is instantaneous SI (identity): ``PRATE`` is an
instantaneous precipitation flux (no de-accumulation), ``DSWRF``/``DLWRF`` are
instantaneous (no de-averaging), and ``SPFH`` 2 m is shipped directly (no
humidity derivation). The fields are split across **two** files per lead — the
primary ``awp130pgrb`` carries T/q/dewpoint/pressure/wind/PRATE, and the
secondary ``awp130bgrb`` carries the surface radiation (``DSWRF``/``DLWRF`` are
absent from the pgrb file) — so a piece fetches at most two ``.idx`` indices
and byte-ranges the messages it needs from each. The 2-D LCC lat/lon comes from
cfgrib on decode, windowed with :func:`cfs.subset.grid2d.subset_2d_grid` via
:func:`cfs.connectors.protocols.grib_idx.read_field_2d`.

Needs the ``forecast`` extra (cfgrib/eccodes). Anonymous, so live-verifiable.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.grib_idx import (
    COMBINED_WIND_CFNAME,
    cycle_for,
    http_range,
    parse_idx,
    read_field_2d,
)
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

GRIB_BASE = "https://noaa-rap-pds.s3.amazonaws.com"

# Surface forcing fields in the awp130 .idx, matched as (grib_var, grib_level).
# All instantaneous SI → identity. `source` is the file the field lives in:
# "pgrb" (primary) or "bgrb" (secondary; carries the surface radiation, which is
# absent from pgrb). Live-probed 2026-06-13.
_FCST_FIELDS: list[tuple[str, str, CanonicalVar, str]] = [
    ("TMP", "2 m above ground", CanonicalVar.AIR_TEMPERATURE, "pgrb"),
    ("SPFH", "2 m above ground", CanonicalVar.SPECIFIC_HUMIDITY, "pgrb"),
    ("DPT", "2 m above ground", CanonicalVar.DEWPOINT_TEMPERATURE, "pgrb"),
    ("PRES", "surface", CanonicalVar.SURFACE_AIR_PRESSURE, "pgrb"),
    ("UGRD", "10 m above ground", CanonicalVar.EASTWARD_WIND, "pgrb"),
    ("VGRD", "10 m above ground", CanonicalVar.NORTHWARD_WIND, "pgrb"),
    ("PRATE", "surface", CanonicalVar.PRECIPITATION_FLUX, "pgrb"),
    ("DSWRF", "surface", CanonicalVar.SHORTWAVE_RADIATION_DOWN, "bgrb"),
    ("DLWRF", "surface", CanonicalVar.LONGWAVE_RADIATION_DOWN, "bgrb"),
]
_MAPPINGS: list[VariableMapping] = [VariableMapping(var, canon) for var, _l, canon, _s in _FCST_FIELDS]

# Lead structure: f00–f21 every cycle; f00–f51 on the 03/09/15/21Z runs.
_MAX_LEAD_STD = 21
_MAX_LEAD_EXTENDED = 51
_EXTENDED_CYCLES = frozenset({3, 9, 15, 21})


def _max_lead(cycle_hour: int) -> int:
    return _MAX_LEAD_EXTENDED if cycle_hour in _EXTENDED_CYCLES else _MAX_LEAD_STD


def _file_url(cycle: datetime, lead: int, source: str) -> str:
    """URL of the awp130 ``pgrb``/``bgrb`` GRIB2 file for ``cycle`` + ``lead``."""
    return (
        f"{GRIB_BASE}/rap.{cycle:%Y%m%d}/"
        f"rap.t{cycle:%H}z.awp130{source}f{lead:02d}.grib2"
    )


@register("rap")
class RAPConnector(BaseForcingConnector):
    slug = "rap"
    display_name = "NOAA RAP atmospheric forecast (13 km, hourly, CONUS)"
    base_url = GRIB_BASE
    protocol = "http"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:awp130_fcst",
                provider=self.slug,
                name="RAP 13 km surface forecast (awp130, CONUS)",
                description=(
                    "NOAA Rapid Refresh surface forcing, byte-range read from the "
                    "GRIB2 awp130 pgrb+bgrb files on the noaa-rap-pds S3 archive. "
                    "The most recent hourly cycle at/before the requested start "
                    "supplies the valid-time forcing (leads to f21, or f51 for the "
                    "03/09/15/21Z runs); instantaneous precipitation flux and "
                    "surface radiation."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.117,  # ~13 km
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-139.0, min_lat=16.0, max_lon=-57.0, max_lat=58.0),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="U.S. Government work / NOAA Open Data (public domain)",
                citation="NOAA NCEP Rapid Refresh (RAP).",
            )
        ]

    @staticmethod
    def _require_cfgrib() -> None:
        try:
            import cfgrib  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "RAP GRIB2 decoding needs the 'forecast' extra: "
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
        selected = [f for f in _FCST_FIELDS if wanted is None or f[2] in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by RAP")

        cycle = cycle_for(time_range.start, step_h=1)
        max_lead = _max_lead(cycle.hour)
        valid_times = pd.date_range(time_range.start, time_range.end, freq="h")
        warnings: list[str] = []

        def _piece(valid):
            lead = int((valid - cycle).total_seconds() // 3600)
            if not (0 <= lead <= max_lead):
                warnings.append(
                    f"RAP lead f{lead:02d} (valid {valid:%Y-%m-%dT%H}) unavailable "
                    f"from cycle {cycle:%Y%m%d %Hz} (max f{max_lead:02d})"
                )
                return None
            per_var: list[xr.Dataset] = []
            # Group by source file so each .idx is fetched at most once per lead.
            for source in ("pgrb", "bgrb"):
                fields = [f for f in selected if f[3] == source]
                if not fields:
                    continue
                url = _file_url(cycle, lead, source)
                idx = parse_idx(http_range(url + ".idx", 0, "").decode())
                # RAP packs 10 m U+V in one GRIB message (shared .idx offset);
                # prefer pulls the right component from that combined message.
                per_var.extend(
                    f for var, level, _c, _s in fields
                    if (f := read_field_2d(url, idx, var, level, var, bbox, label="RAP",
                                           prefer=COMBINED_WIND_CFNAME.get(var))) is not None
                )
            if not per_var:
                return None
            # per_var are disjoint single-variable cubes sharing the same grid coords.
            merged = xr.merge(per_var, join="inner", compat="override")
            return merged.expand_dims(time=[pd.Timestamp(valid)])

        pieces = await self._gather_pieces([partial(_piece, v) for v in valid_times])
        if not pieces:
            raise SubsetError(
                f"No RAP data in [{time_range.start}, {time_range.end}] from cycle "
                f"{cycle:%Y%m%d %Hz} (archive retains a long history; very old cycles age off)"
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
                f"RAP awp130 (pgrb+bgrb) via noaa-rap-pds S3 byte-range (cfgrib); "
                f"cycle {cycle:%Y%m%d %Hz}; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings or None,
            ydim="y",
            xdim="x",
        )
