# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""MSWEP connector — Multi-Source Weighted-Ensemble Precipitation (via rclone/Drive).

MSWEP (GloH2O) is a global 0.1° gauge+satellite+reanalysis-merged precipitation
product, distributed only through a Google Drive folder shared with registered
users. Access is via the ``rclone`` CLI (see :mod:`cfs.connectors.protocols.rclone`);
this connector streams the per-timestep NetCDFs, subsets to bbox + time, and
harmonizes ``precipitation`` (mm per step) to the canonical ``precipitation_flux``.

Temporal resolution is exposed as the product id (``mswep:daily``,
``mswep:3hourly``), each with its own constant conversion (daily ``/86400``,
3-hourly ``/10800``). The MSWEP version (``V280``/``V300``) and the rclone remote
name are connector ``config`` knobs (remote also read from ``MSWEP_RCLONE_REMOTE``).

Out-of-band access: needs ``rclone`` installed + GloH2O-granted Drive access, so
this connector is offline-verified (path/conversion logic) and flagged for a
first authenticated run. The Monthly resolution is deferred (variable-length
month → non-constant flux scale).
"""

from __future__ import annotations

import io
import os
import time

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.rclone import RcloneMixin
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

VERSIONS = {"V280": "MSWEP_V280", "V300": "MSWEP_V300"}
_PRECIP_CANDIDATES = ("precipitation", "precip")

# resolution → (remote subdir, seconds-per-step, temporal enum)
RESOLUTIONS = {
    "daily": ("Daily", 86400.0, TemporalResolution.DAILY),
    "3hourly": ("3hourly", 10800.0, TemporalResolution.THREE_HOURLY),
}


def _detect_precip(ds) -> str | None:
    for cand in _PRECIP_CANDIDATES:
        if cand in ds.data_vars:
            return cand
    matches = [v for v in ds.data_vars if "precip" in str(v).lower()]
    return matches[0] if matches else None


def _latlon_names(ds) -> tuple[str, str]:
    lat = "latitude" if "latitude" in ds.coords else "lat"
    lon = "longitude" if "longitude" in ds.coords else "lon"
    return lat, lon


@register("mswep")
class MSWEPConnector(RcloneMixin, BaseForcingConnector):
    slug = "mswep"
    display_name = "MSWEP (Multi-Source Weighted-Ensemble Precipitation, 0.1°)"
    base_url = "gdrive://MSWEP (rclone --drive-shared-with-me)"
    protocol = "rclone"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = self.config or {}
        self.remote = cfg.get("remote") or os.environ.get("MSWEP_RCLONE_REMOTE", "GoogleDrive")
        self.version = cfg.get("version", "V300")
        if self.version not in VERSIONS:
            raise SubsetError(f"Unknown MSWEP version '{self.version}' (use V280/V300)")

    async def list_products(self) -> list[ForcingProduct]:
        products = []
        for res, (_subdir, _secs, temporal) in RESOLUTIONS.items():
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:{res}",
                    provider=self.slug,
                    name=f"MSWEP {self.version} {res} precipitation (0.1°)",
                    description=(
                        f"MSWEP {self.version} {res} global precipitation via rclone / "
                        "shared Google Drive (GloH2O). Precip-only."
                    ),
                    variables=[
                        ProductVariable(
                            canonical=CanonicalVar.PRECIPITATION_FLUX, source_name="precipitation"
                        )
                    ],
                    resolution_deg=0.1,
                    crs="EPSG:4326",
                    bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                    temporal=TemporalExtent(resolution=temporal),
                    protocol=Protocol.REST,
                    license="MSWEP — free for research after GloH2O registration.",
                    citation="Beck et al. (2019), MSWEP V2, BAMS 100:473-500.",
                )
            )
        return products

    def _relative_paths(self, resolution: str, time_range: TimeRange):
        """Yield (timestamp, remote relative path) for the requested window."""
        import pandas as pd

        folder = VERSIONS[self.version]
        subdir = RESOLUTIONS[resolution][0]
        if resolution == "daily":
            for d in pd.date_range(time_range.start.date(), time_range.end.date(), freq="D"):
                doy = d.timetuple().tm_yday
                yield d, f"{folder}/{subdir}/{d.year}/{doy:03d}.nc"
        else:  # 3hourly
            for ts in pd.date_range(time_range.start, time_range.end, freq="3h"):
                doy = ts.timetuple().tm_yday
                yield ts, f"{folder}/{subdir}/{ts.year}/{doy:03d}{ts.hour:02d}.nc"

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
        resolution = product_id.split(":", 1)[1]
        if resolution not in RESOLUTIONS:
            raise SubsetError(f"Unknown MSWEP resolution '{resolution}'")
        if variables is not None and CanonicalVar.PRECIPITATION_FLUX not in set(variables):
            raise SubsetError("MSWEP offers only precipitation_flux")
        settings = get_settings()
        self._guard_area(bbox, settings)
        # Fail fast on a setup problem (rclone missing, or the Drive remote not
        # configured) so it surfaces as a clear RegistrationRequiredError rather
        # than being mistaken for every timestep being "unavailable" below.
        self._require_rclone_remote(self.remote)

        seconds = RESOLUTIONS[resolution][1]
        warnings: list[str] = []
        pieces = []
        for _ts, rel in self._relative_paths(resolution, time_range):
            try:
                raw = self._rclone_cat(self.remote, rel)
            except ConnectorError as e:  # skip a single missing/failed step, keep going
                warnings.append(f"{rel} unavailable: {type(e).__name__}")
                continue
            ds = xr.open_dataset(io.BytesIO(raw), engine="h5netcdf")
            var = _detect_precip(ds)
            if var is None:
                continue
            lat_name, lon_name = _latlon_names(ds)
            ds = ds[[var]].rename({var: "precipitation"})
            plan = plan_bbox_subset(ds, bbox, lat_name=lat_name, lon_name=lon_name)
            ds = apply_bbox_subset(ds, plan, lat_name=lat_name, lon_name=lon_name)
            if "time" in ds.dims:
                ds = ds.sel(time=slice(time_range.start, time_range.end))
            pieces.append((ds.rename({lat_name: "lat", lon_name: "lon"}) if lat_name != "lat" else ds).load())

        if not pieces:
            raise SubsetError(
                f"No MSWEP {resolution} data in [{time_range.start}, {time_range.end}]"
            )
        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]

        mapping = [
            VariableMapping(
                "precipitation", CanonicalVar.PRECIPITATION_FLUX,
                scale=1.0 / seconds, note=f"MSWEP {resolution} (mm/step) -> flux (kg m-2 s-1)",
            )
        ]
        canonical = harmonize(ds_all, mapping, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=f"MSWEP {self.version} {resolution} via rclone/Drive; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=warnings,
        )
