# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NWM Operational connector — NOAA National Water Model real-time forcing (S3).

Acquires forcing data for the operational National Water Model (NWM) from the
public ``noaa-nwm-pds`` S3 bucket. Unlike the retrospective AORC, this bucket
maintains a rolling **4-week archive** of real-time NetCDF files.

Files are organized by date, forecast configuration, and (for forecasts) lead
hour:
``s3://noaa-nwm-pds/nwm.YYYYMMDD/forcing_{config}/nwm.t{cycle}z.{config}.forcing.{lead}.conus.nc``

Configurations (exposed as product ids):
  * ``nwm_operational:analysis_assim`` — real-time analysis. Each valid hour is
    served by that hour's ``tm00`` file (the best estimate of forcing at the
    cycle time), which maps cleanly to CFS's "data at a valid time" model.
  * ``nwm_operational:short_range`` — 18-hour deterministic forecast. Hourly
    cycles, lead files ``f001``…``f018``.
  * ``nwm_operational:medium_range`` — 10-day deterministic forecast. 00/06/12/18Z
    cycles, lead files ``f001``…``f240``.

Forecast model (short/medium range): like ``gfs``, CFS picks the most recent
cycle at or before the requested start and serves each valid hour from that
cycle's lead file (``lead = valid - cycle``). There is **no ``f000``** forcing
file, so a valid hour equal to the cycle time is unavailable (warned) — use
``analysis_assim`` for the nowcast. Lead hours beyond the configuration's
horizon are skipped (warned). The CONUS forcing has no ensemble members (the
NWM ensemble lives in the model output, not the forcing input). Live-probed
against ``noaa-nwm-pds`` 2026-06-15.

The grid is the standard NWM 1km LCC (same as ``aorc_nwm``).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

import numpy as np
import structlog

from cfs.connectors.base import BaseForcingConnector
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
from cfs.subset.grid2d import subset_2d_grid

if TYPE_CHECKING:
    import xarray as xr


logger = structlog.get_logger()

BUCKET = "noaa-nwm-pds"

# NWM v3.0 1 km LCC projection (same grid as aorc_nwm). The operational LDASIN
# forcing files carry only x/y (metres) + a crs — no lat/lon — so CFS generates
# 2-D lat/lon from these projection parameters.
_PROJ_LCC = (
    "+proj=lcc +lat_1=30 +lat_2=60 +lat_0=40 +lon_0=-97 "
    "+x_0=0 +y_0=0 +a=6370000 +b=6370000 +units=m +no_defs"
)

# Native NWM forcing names (LDASIN) → canonical. Same as AORC-NWM.
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("T2D", CanonicalVar.AIR_TEMPERATURE),
    VariableMapping("Q2D", CanonicalVar.SPECIFIC_HUMIDITY),
    VariableMapping("PSFC", CanonicalVar.SURFACE_AIR_PRESSURE),
    VariableMapping("U2D", CanonicalVar.EASTWARD_WIND),
    VariableMapping("V2D", CanonicalVar.NORTHWARD_WIND),
    VariableMapping("SWDOWN", CanonicalVar.SHORTWAVE_RADIATION_DOWN),
    VariableMapping("LWDOWN", CanonicalVar.LONGWAVE_RADIATION_DOWN),
    VariableMapping("RAINRATE", CanonicalVar.PRECIPITATION_FLUX),
]

# Configs exposed as product ids. analysis_assim serves each valid hour's tm00
# file; the forecasts resolve a valid time to (most-recent cycle <= start, lead).
# step_h is the cycle cadence; max_lead the forecast horizon (no f000). Live-
# probed against noaa-nwm-pds 2026-06-15: short_range hourly cycles f001-f018,
# medium_range 00/06/12/18Z cycles f001-f240, neither with ensemble members.
_ANALYSIS = "analysis"
_FORECAST = "forecast"
CONFIGS: dict[str, dict] = {
    "analysis_assim": {"kind": _ANALYSIS},
    "short_range": {"kind": _FORECAST, "step_h": 1, "max_lead": 18},
    "medium_range": {"kind": _FORECAST, "step_h": 6, "max_lead": 240},
}


def _floor_cycle(ts: datetime, step_h: int) -> datetime:
    """Most recent cycle at or before ``ts`` for a ``step_h``-hourly cadence."""
    return ts.replace(hour=(ts.hour // step_h) * step_h, minute=0, second=0, microsecond=0)


def _analysis_path(ts: datetime) -> str:
    """tm00 forcing file valid at hour ``ts`` (analysis_assim)."""
    return (
        f"{BUCKET}/nwm.{ts:%Y%m%d}/forcing_analysis_assim/"
        f"nwm.t{ts:%H}z.analysis_assim.forcing.tm00.conus.nc"
    )


def _forecast_path(conf: str, cycle: datetime, lead: int) -> str:
    """Lead ``f{lead:03d}`` forcing file from ``cycle`` (short/medium range)."""
    return (
        f"{BUCKET}/nwm.{cycle:%Y%m%d}/forcing_{conf}/"
        f"nwm.t{cycle:%H}z.{conf}.forcing.f{lead:03d}.conus.nc"
    )


@register("nwm_operational")
class NWMOperationalConnector(BaseForcingConnector):
    slug = "nwm_operational"
    display_name = "NWM Operational Forcing (1 km, hourly, via S3)"
    base_url = f"s3://{BUCKET}"
    protocol = "s3_direct"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._fs = None

    def _filesystem(self):
        if self._fs is None:
            try:
                import s3fs
            except ImportError as e:  # pragma: no cover
                raise MissingExtraError("NWM Operational needs s3fs (the 'climate' extra).") from e
            self._fs = s3fs.S3FileSystem(anon=True)
        return self._fs

    def _generate_grid(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """2-D lat/lon (degrees) from the LCC x/y metres via pyproj."""
        try:
            from pyproj import Transformer
        except ImportError as e:  # pragma: no cover
            raise MissingExtraError("NWM Operational needs pyproj (the 'earthdata' extra).") from e
        xx, yy = np.meshgrid(x, y)
        lon, lat = Transformer.from_crs(_PROJ_LCC, "EPSG:4326", always_xy=True).transform(xx, yy)
        return lat, lon

    async def list_products(self) -> list[ForcingProduct]:
        descriptions = {
            "analysis_assim": (
                "Real-time NWM analysis forcing (tm00 best-estimate at each valid "
                "hour) on the 1km CONUS LCC grid. Rolling 4-week archive."
            ),
            "short_range": (
                "NWM 18-hour deterministic forecast forcing (hourly cycles, "
                "f001-f018) on the 1km CONUS LCC grid. Rolling 4-week archive."
            ),
            "medium_range": (
                "NWM 10-day deterministic forecast forcing (00/06/12/18Z cycles, "
                "f001-f240) on the 1km CONUS LCC grid. Rolling 4-week archive."
            ),
        }
        products = []
        for pid in CONFIGS:
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{pid}",
                    provider=self.slug,
                    name=f"NWM Operational Forcing ({pid})",
                    description=descriptions[pid],
                    variables=[
                        ProductVariable(canonical=m.canonical, source_name=m.source_name)
                        for m in _MAPPINGS
                    ],
                    resolution_deg=0.00833,
                    crs="EPSG:4326",
                    bbox=BoundingBox(min_lon=-134.0, min_lat=21.0, max_lon=-60.0, max_lat=53.0),
                    temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                    protocol=Protocol.S3_DIRECT,
                    license="U.S. Government work / NOAA Open Data (public domain)",
                    citation="NOAA National Water Model (NWM) operational data.",
                )
            )
        return products

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
        conf = product_id.split(":", 1)[1]
        spec = CONFIGS[conf]
        settings = get_settings()
        self._guard_area(bbox, settings)

        fs = self._filesystem()

        # Resolve the requested window to (valid_time, S3 path) work items. Forcing
        # files are hourly. analysis_assim reads each hour's tm00 file; the forecasts
        # resolve each valid hour to a lead file from the cycle floored to the start.
        steps = []
        curr = time_range.start
        while curr <= time_range.end:
            steps.append(curr)
            curr += timedelta(hours=1)

        warnings: list[str] = []
        cycle: datetime | None = None
        work: list[tuple[datetime, str]] = []
        if spec["kind"] == _ANALYSIS:
            work = [(ts, _analysis_path(ts)) for ts in steps]
        else:
            step_h, max_lead = spec["step_h"], spec["max_lead"]
            cycle = _floor_cycle(time_range.start, step_h)
            for ts in steps:
                lead = int((ts - cycle).total_seconds() // 3600)
                if 1 <= lead <= max_lead:
                    work.append((ts, _forecast_path(conf, cycle, lead)))
                else:
                    warnings.append(
                        f"NWM {conf} lead f{lead:03d} (valid {ts:%Y-%m-%dT%H}) "
                        f"unavailable from cycle {cycle:%Y%m%d %Hz}"
                    )

        # NWM LDASIN files carry only x/y (no lat/lon); generate the 2-D lat/lon
        # once from the LCC projection and reuse it for every hour.
        grid = None
        for _ts, p in work:
            if fs.exists(p):
                with fs.open(p) as fh:
                    ds0 = xr.open_dataset(fh, engine="h5netcdf")
                    grid = self._generate_grid(ds0["x"].values, ds0["y"].values)
                break
        if grid is None:
            raise SubsetError(
                f"No NWM Operational data in [{time_range.start}, {time_range.end}] for {conf}. "
                "Note: the noaa-nwm-pds archive only covers roughly the last 4 weeks."
            )
        glat, glon = grid

        def _piece(item: tuple[datetime, str]):
            ts, path = item
            try:
                ds = xr.open_dataset(fs.open(path), engine="h5netcdf", chunks={})
                ds = ds.assign_coords(
                    latitude=(("y", "x"), glat), longitude=(("y", "x"), glon)
                )
                ds = subset_2d_grid(ds, bbox, lat_name="latitude", lon_name="longitude")
                if "time" not in ds.dims:
                    ds = ds.expand_dims(time=[ts])
                return ds.load()
            except Exception as e:
                logger.debug("nwm_operational step skip", path=path, error=str(e))
                return None

        pieces = await self._gather_pieces([partial(_piece, item) for item in work])

        if not pieces:
            raise SubsetError(
                f"No NWM Operational data in [{time_range.start}, {time_range.end}] for {conf}. "
                "Note: The archive only covers the last 4 weeks."
            )

        ds_all = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")

        provenance = f"NWM Operational Forcing {conf} (S3)"
        if cycle is not None:
            provenance += f"; cycle {cycle:%Y%m%d %Hz}"
        provenance += "; canonical-v1"

        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=provenance,
            t0=t0,
            settings=settings,
            lazy=False,
            ydim="y",
            xdim="x",
            extra_warnings=warnings or None,
        )
