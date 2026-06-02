# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NWM Operational connector — NOAA National Water Model real-time forcing (S3).

Acquires forcing data for the operational National Water Model (NWM) from the
public ``noaa-nwm-pds`` S3 bucket. Unlike the retrospective AORC, this bucket
maintains a rolling **4-week archive** of real-time NetCDF files.

Files are organized by date and forecast configuration:
``s3://noaa-nwm-pds/nwm.YYYYMMDD/{config}/nwm.t{cycle}z.{config}.forcing.conus.nc``

Configurations (exposed as product ids):
  * ``nwm_operational:analysis_assim`` (Real-time analysis)
  * ``nwm_operational:short_range`` (18-hour forecast)
  * ``nwm_operational:medium_range`` (10-day forecast)

The grid is the standard NWM 1km LCC (same as ``aorc_nwm``).
"""

from __future__ import annotations

import time
from datetime import timedelta

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

# Only the analysis_assim configuration is exposed: it provides the best estimate
# of forcing at each valid hour via the `tm00` file, which maps cleanly to CFS's
# "data at a valid time" model. The short_range / medium_range *forecasts* (cycle ×
# lead-hour) need a proper valid-time→(cycle, lead) resolver and are out of scope.
CONFIGS = {
    "analysis_assim": "analysis_assim",
}


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
        products = []
        for pid in CONFIGS:
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{pid}",
                    provider=self.slug,
                    name=f"NWM Operational Forcing ({pid})",
                    description=(
                        f"Real-time NWM operational forcing for {pid} "
                        "on the 1km CONUS LCC grid. Rolling 4-week archive."
                    ),
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
    ) -> tuple[object, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        conf = product_id.split(":", 1)[1]
        settings = get_settings()
        self._guard_area(bbox, settings)

        fs = self._filesystem()

        def _path(ts):
            # analysis_assim: the tm00 file is the analysis valid at cycle hour HH,
            # i.e. the forcing for that valid time. Filename confirmed against the
            # live noaa-nwm-pds bucket.
            return (
                f"{BUCKET}/nwm.{ts.strftime('%Y%m%d')}/forcing_{conf}/"
                f"nwm.t{ts.strftime('%H')}z.{conf}.forcing.tm00.conus.nc"
            )

        # Forcing files are hourly.
        steps = []
        curr = time_range.start
        while curr <= time_range.end:
            steps.append(curr)
            curr += timedelta(hours=1)

        # NWM LDASIN files carry only x/y (no lat/lon); generate the 2-D lat/lon
        # once from the LCC projection and reuse it for every hour.
        grid = None
        for ts in steps:
            p = _path(ts)
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

        def _piece(ts):
            path = _path(ts)
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

        pieces = await self._gather_pieces([lambda ts=ts: _piece(ts) for ts in steps])

        if not pieces:
            raise SubsetError(
                f"No NWM Operational data in [{time_range.start}, {time_range.end}] for {conf}. "
                "Note: The archive only covers the last 4 weeks."
            )
        
        ds_all = xr.concat(pieces, dim="time") if len(pieces) > 1 else pieces[0]
        canonical = harmonize(ds_all, _MAPPINGS, requested=variables,
                              lat_name="latitude", lon_name="longitude")
        
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"NWM Operational Forcing {conf} (S3); canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            ydim="y",
            xdim="x",
        )
