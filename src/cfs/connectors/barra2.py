# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""BARRA2 connector — BoM Australian Regional Reanalysis v2 via NCI THREDDS.

BARRA-R2 is the Bureau of Meteorology's ~12 km (AUS-11, 0.11°) hourly regional
reanalysis over Australia and surrounding seas (1979→present), published openly on
the NCI THREDDS server (project ``ob53``) following the CORDEX-CMIP data reference
syntax. It fills CFS's Australia/Oceania gap.

Access is anonymous and uses the THREDDS **NetcdfSubset** service (``ncss``): the
server does the bbox + time subset and returns a clean NetCDF over plain HTTP —
one request per variable per **month**. (The OPeNDAP endpoint exists too, but its
DAP2 responses truncate under concurrent reads on this server; ncss is the robust
acquire-and-subset path and keeps the spatial subset server-side, which is exactly
the CFS boundary.) The native grid is a regular 1-D ``lat``/``lon`` (0–360
longitude — requested longitudes are normalized to that convention).

Every field uses CORDEX/CMIP CF names already in canonical SI, so all mappings are
identity — including ``pr`` (``precipitation_flux``, kg m⁻² s⁻¹) and ``huss``
(specific humidity). No dewpoint is published (``tdps`` absent), so dewpoint is
simply not offered.

BARRA2 stamps instantaneous fields on the hour (``tas``/``ps``/``uas``/``vas``/
``huss`` at HH:00) but hourly *means* at the interval midpoint (``pr``/``rsds``/
``rlds`` at HH:30); times are floored to the hour so a period-mean is labelled at
the start of the hour it covers and shares the instantaneous axis. Variable names,
units, the regular grid, monthly files, the ``latest`` version alias, and live
ncss reads were all confirmed against the NCI store.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

import structlog

from cfs.connectors.base import BaseForcingConnector
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
from cfs.subset.canonical import VariableMapping, harmonize

logger = structlog.get_logger()

# NCI THREDDS NetcdfSubset root for BARRA-R2 hourly (AUS-11, ERA5/historical/hres).
NCSS_ROOT = (
    "https://thredds.nci.org.au/thredds/ncss/grid/ob53/BARRA2/output/reanalysis/"
    "AUS-11/BOM/ERA5/historical/hres/BARRA-R2/v1/1hr"
)
_FILE_STEM = "AUS-11_ERA5_historical_hres_BOM_BARRA-R2_v1_1hr"

# BARRA-R2 native (CORDEX/CMIP CF) names → canonical. All identity (already SI).
_MAPPINGS: list[VariableMapping] = [
    VariableMapping("tas", CanonicalVar.AIR_TEMPERATURE),          # 1.5 m temperature, K
    VariableMapping("huss", CanonicalVar.SPECIFIC_HUMIDITY),       # near-surface q, kg/kg
    VariableMapping("ps", CanonicalVar.SURFACE_AIR_PRESSURE),      # Pa
    VariableMapping("uas", CanonicalVar.EASTWARD_WIND),            # 10 m u, m/s
    VariableMapping("vas", CanonicalVar.NORTHWARD_WIND),           # 10 m v, m/s
    VariableMapping("rsds", CanonicalVar.SHORTWAVE_RADIATION_DOWN),   # W/m2
    VariableMapping("rlds", CanonicalVar.LONGWAVE_RADIATION_DOWN),    # W/m2
    VariableMapping("pr", CanonicalVar.PRECIPITATION_FLUX),        # already kg m-2 s-1
]


def _months(time_range: TimeRange) -> list[tuple[int, int]]:
    """Enumerate (year, month) pairs spanning the request, inclusive."""
    y, m = time_range.start.year, time_range.start.month
    end_y, end_m = time_range.end.year, time_range.end.month
    out: list[tuple[int, int]] = []
    while (y, m) <= (end_y, end_m):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ncss_url(
    var: str, year: int, month: int, bbox: BoundingBox, time_start: str, time_end: str
) -> str:
    """Build a NetcdfSubset request for one variable's monthly file + bbox + window.

    Longitudes are normalized to the store's 0–360 convention. BARRA2's domain
    (≈88–207°E) does not straddle the prime meridian, so the normalized west/east
    stay ordered and a request that straddles 180° in −180/180 terms is handled
    naturally (180° is interior to 0–360 here). The ``time_start``/``time_end``
    window is required — ncss returns only the file's *last* timestep otherwise;
    ncss clips it to the times the targeted monthly file actually holds.
    """
    ym = f"{year}{month:02d}"
    west = bbox.min_lon % 360.0
    east = bbox.max_lon % 360.0
    return (
        f"{NCSS_ROOT}/{var}/latest/{var}_{_FILE_STEM}_{ym}-{ym}.nc"
        f"?var={var}&north={bbox.max_lat}&south={bbox.min_lat}"
        f"&west={west}&east={east}"
        f"&time_start={time_start}&time_end={time_end}&accept=netcdf4"
    )


@register("barra2")
class BARRA2Connector(BaseForcingConnector):
    slug = "barra2"
    display_name = "BoM BARRA-R2 (Australian Regional Reanalysis v2, ~12 km, hourly)"
    base_url = NCSS_ROOT
    protocol = "rest"

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:barra_r2",
                provider=self.slug,
                name="BARRA-R2 hourly surface forcing (~12 km, Australia)",
                description=(
                    "Bureau of Meteorology BARRA2 regional reanalysis (BARRA-R2, "
                    "AUS-11 0.11° grid) over Australia and surrounding seas, via the "
                    "anonymous NCI THREDDS NetcdfSubset service (project ob53)."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.11,  # ~12 km
                crs="EPSG:4326",
                # AUS-11 domain ~88.5°E–207.4°E (i.e. to 152.6°W), 58°S–13°N —
                # crosses the antimeridian, so max_lon wraps to a negative value.
                bbox=BoundingBox(min_lon=88.48, min_lat=-57.97, max_lon=-152.61, max_lat=12.98),
                temporal=TemporalExtent(resolution=TemporalResolution.HOURLY),
                protocol=Protocol.REST,
                license="Bureau of Meteorology / NCI, CC-BY 4.0.",
                citation="Su et al. (2022), BARRA2, Bureau Research Report 067.",
            )
        ]

    def _cache_dir(self) -> Path:
        d = Path(os.path.expanduser(get_settings().cache_dir)) / self.slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _download(self, url: str, target: Path) -> Path:
        """Download a NetcdfSubset result to ``target`` (cached), with retries."""
        if target.exists() and target.stat().st_size > 0:
            return target
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                tmp = target.with_suffix(target.suffix + ".part")
                urllib.request.urlretrieve(url, tmp)  # noqa: S310 - https NCI THREDDS
                tmp.replace(target)
                return target
            except Exception as e:  # noqa: BLE001 - retry transient HTTP/subset errors
                last = e
                logger.warning("barra2 ncss download failed", attempt=attempt, error=str(e))
        raise ConnectorError("barra2", f"NetcdfSubset download failed for {url}: {last}")

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

        wanted = set(variables) if variables else None
        selected = [m for m in _MAPPINGS if wanted is None or m.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by BARRA2")

        months = _months(time_range)
        cache = self._cache_dir()
        bbox_key = hashlib.md5(  # noqa: S324 - cache key, not security
            f"{bbox.min_lon},{bbox.min_lat},{bbox.max_lon},{bbox.max_lat}".encode(),
            usedforsecurity=False,
        ).hexdigest()[:8]
        # Pad the request end by an hour so the trailing period-mean stamp (HH:30,
        # covering the final hour) is fetched; the final .sel() trims back exactly.
        ts_iso = _iso(time_range.start)
        te_iso = _iso(time_range.end + timedelta(hours=1))
        win_key = hashlib.md5(  # noqa: S324 - cache key, not security
            f"{ts_iso},{te_iso}".encode(), usedforsecurity=False
        ).hexdigest()[:8]

        # One ncss request per (variable, month): the server subsets to the bbox
        # and returns a clean NetCDF (cached). Downloads run concurrently (plain
        # HTTP, thread-safe); the NetCDF *opens* happen serially below because the
        # HDF5/netcdf-c library is not thread-safe (concurrent opens segfault).
        items = [(m.source_name, y, mo) for m in selected for (y, mo) in months]

        def _dl(var: str, year: int, month: int):
            url = _ncss_url(var, year, month, bbox, ts_iso, te_iso)
            target = cache / f"{self.slug}_{var}_{year}{month:02d}_{bbox_key}_{win_key}.nc"
            return (var, self._download(url, target))

        downloads = await self._gather_pieces([lambda it=it: _dl(*it) for it in items])
        if not downloads:
            raise SubsetError(f"No BARRA2 data in [{time_range.start}, {time_range.end}]")

        # Open the cached files serially, then group by variable, concat each over
        # time, and merge the per-variable cubes onto the shared grid.
        by_var: dict[str, list] = {}
        for var, target in downloads:
            ds = xr.open_dataset(target)[[var]]
            drop = [c for c in ds.coords if c not in ("time", "lat", "lon")]
            if drop:
                ds = ds.drop_vars(drop, errors="ignore")
            # Floor period-means (HH:30) to the hour so they share the
            # instantaneous (HH:00) axis on merge — see module docstring.
            if "time" in ds.coords:
                ds = ds.assign_coords(time=ds["time"].dt.floor("h"))
            by_var.setdefault(var, []).append(ds)
        cubes = [
            (xr.concat(v, dim="time").sortby("time") if len(v) > 1 else v[0])
            for v in by_var.values()
        ]
        ds_all = xr.merge(cubes, join="inner") if len(cubes) > 1 else cubes[0]
        ds_all = ds_all.sel(time=slice(time_range.start, time_range.end))
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No BARRA2 timesteps in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, selected, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance="BARRA-R2 via NCI THREDDS NetcdfSubset (ob53); per-variable monthly files; canonical-v1",
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                f"BARRA2 issues one NetcdfSubset request per (variable, month): "
                f"{len(selected)}×{len(months)} requested, up to {settings.fetch_concurrency} "
                "concurrently."
            ],
        )
