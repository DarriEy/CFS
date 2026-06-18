# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""AgERA5 connector — C3S agrometeorological indicators via the Copernicus CDS.

AgERA5 is the daily agrometeorological reanalysis derived from ERA5, distributed
on the CDS (``sis-agrometeorological-indicators``) as global 0.1° daily NetCDF —
one file per variable per day, returned in a zip. Like WFDE5 it offers no
server-side area subset on the legacy form, so this connector downloads the
requested variable/day files, opens them lazily, subsets locally, and harmonizes.

**Partial forcing set.** AgERA5 is agromet-focused, so it carries only a subset
of the eight canonical forcing variables — ``air_temperature`` (2 m daily mean),
``precipitation_flux`` (mm/day), ``surface_downwelling_shortwave_flux``
(``Solar_Radiation_Flux``, J m⁻² day⁻¹ → W m⁻²), ``wind_speed`` (10 m scalar
mean), and ``dewpoint_temperature`` (2 m daily mean). It does **not** publish
downwelling longwave, surface pressure, or specific humidity, so those cannot be
served from AgERA5 alone (derive specific humidity downstream from dewpoint if a
pressure field is available from another source).

Auth-gated: needs CDS credentials (``~/.cdsapirc`` or ``CDSAPI_URL``/
``CDSAPI_KEY``) and acceptance of the dataset licence on the CDS website. Since
the 26 Sep 2024 CDS migration the credential is a personal access token (the new
``~/.cdsapirc`` has only ``url`` + ``key``, no UID field), and the licence is
CC-BY-4.0 (Copernicus migration, 2 Jul 2025).

.. note::
   Live-verified 2026-06-18 via a credentialed CDS round-trip (3 vars, 1 day,
   Alps bbox): the documented v2.0 NetCDF names and request schema (``variable``
   + ``statistic`` + ``version`` + ``area`` + ``format=zip``, one daily ``.nc``
   per variable) decode and harmonize to sane canonical values (air_temperature
   282.9–294.2 K, shortwave 247.5–333.9 W m⁻², precip → 9e-5 kg m⁻² s⁻¹).
"""

from __future__ import annotations

import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from cfs.connectors.base import BaseForcingConnector
from cfs.connectors.protocols.cds_api import CDSAPIMixin
from cfs.core.config import get_settings
from cfs.core.exceptions import SubsetError
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

CDS_DATASET = "sis-agrometeorological-indicators"
_DEFAULT_VERSION = "2_0"


@dataclass(frozen=True)
class _AgVar:
    """One AgERA5 variable: CDS request name (+ optional statistic) → NetCDF name."""

    request_name: str
    nc_name: str
    canonical: CanonicalVar
    statistic: str | None = None
    scale: float = 1.0
    offset: float = 0.0
    note: str = ""


# The mappable subset of canonical forcing variables AgERA5 provides.
_VARS: list[_AgVar] = [
    _AgVar("2m_temperature", "Temperature_Air_2m_Mean_24h", CanonicalVar.AIR_TEMPERATURE,
           statistic="24_hour_mean"),
    _AgVar("2m_dewpoint_temperature", "Dewpoint_Temperature_2m_Mean", CanonicalVar.DEWPOINT_TEMPERATURE,
           statistic="24_hour_mean"),
    _AgVar("10m_wind_speed", "Wind_Speed_10m_Mean", CanonicalVar.WIND_SPEED,
           statistic="24_hour_mean"),
    _AgVar("precipitation_flux", "Precipitation_Flux", CanonicalVar.PRECIPITATION_FLUX,
           scale=1.0 / 86400.0, note="AgERA5 daily precipitation (mm/day) -> flux (kg m-2 s-1)"),
    _AgVar("solar_radiation_flux", "Solar_Radiation_Flux", CanonicalVar.SHORTWAVE_RADIATION_DOWN,
           scale=1.0 / 86400.0, note="AgERA5 daily solar radiation (J m-2 day-1) -> flux (W m-2)"),
]

_MAPPINGS: list[VariableMapping] = [
    VariableMapping(v.nc_name, v.canonical, scale=v.scale, offset=v.offset, note=v.note)
    for v in _VARS
]


def _days(time_range: TimeRange) -> list[tuple[int, int, list[str]]]:
    """Group the request into (year, month, [day strings]) for CDS retrieves."""
    import calendar

    out: list[tuple[int, int, list[str]]] = []
    y, m = time_range.start.year, time_range.start.month
    end_y, end_m = time_range.end.year, time_range.end.month
    while (y, m) <= (end_y, end_m):
        first = time_range.start.day if (y, m) == (time_range.start.year, time_range.start.month) else 1
        last = (time_range.end.day if (y, m) == (end_y, end_m)
                else calendar.monthrange(y, m)[1])
        out.append((y, m, [f"{d:02d}" for d in range(first, last + 1)]))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _extract_ncs(zip_path: Path, dest: Path) -> list[Path]:
    """Extract every .nc member of a CDS zip into ``dest`` (idempotent)."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".nc"):
                continue
            target = dest / Path(member).name
            if not (target.exists() and target.stat().st_size > 0):
                with zf.open(member) as src, open(target, "wb") as fh:
                    fh.write(src.read())
            out.append(target)
    return out


@register("agera5")
class AgERA5Connector(CDSAPIMixin, BaseForcingConnector):
    slug = "agera5"
    display_name = "AgERA5 agrometeorological indicators (0.1°, daily, via CDS)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    @property
    def _version(self) -> str:
        return str(self.config.get("version", _DEFAULT_VERSION))

    async def list_products(self) -> list[ForcingProduct]:
        return [
            ForcingProduct(
                id=f"{self.slug}:daily",
                provider=self.slug,
                name="AgERA5 daily agrometeorological indicators (0.1° global)",
                description=(
                    "C3S AgERA5 daily agrometeorological indicators derived from ERA5, "
                    "via the Copernicus CDS. Partial forcing set (no longwave / surface "
                    "pressure / specific humidity); downloaded per variable/day and "
                    "subset locally."
                ),
                variables=[
                    ProductVariable(canonical=m.canonical, source_name=m.source_name)
                    for m in _MAPPINGS
                ],
                resolution_deg=0.1,
                crs="EPSG:4326",
                bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
                temporal=TemporalExtent(resolution=TemporalResolution.DAILY),
                protocol=Protocol.REST,
                license="CC-BY-4.0 (Copernicus, since 2025-07-02); accept on CDS.",
                citation="Boogaard et al. / C3S AgERA5 (DOI 10.24381/cds.6c68c9bb).",
            )
        ]

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

        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by AgERA5")

        cache = self._cds_cache_dir()
        extract_dir = cache / f"extracted_v{self._version}"
        extract_dir.mkdir(parents=True, exist_ok=True)

        pieces = []
        for year, month, day_list in _days(time_range):
            var_cubes = []
            for v in selected:
                request: dict = {
                    "variable": v.request_name,
                    "year": str(year),
                    "month": f"{month:02d}",
                    "day": day_list,
                    "version": self._version,
                    "area": self._cds_area(bbox),
                    "format": "zip",
                }
                if v.statistic is not None:
                    request["statistic"] = v.statistic
                target = cache / f"{self.slug}_{v.nc_name}_{year}{month:02d}_v{self._version}.zip"
                await self._cds_retrieve(CDS_DATASET, request, target)
                ncs = _extract_ncs(target, extract_dir)
                if not ncs:
                    raise SubsetError(f"AgERA5 archive for {v.nc_name} held no .nc file")
                # One .nc per day → concatenate over time for this variable.
                daily = [xr.open_dataset(p) for p in sorted(ncs)]
                cube = xr.concat(daily, dim="time").sortby("time") if len(daily) > 1 else daily[0]
                var_cubes.append(cube[[v.nc_name]] if v.nc_name in cube else cube)

            ds = xr.merge(var_cubes, join="inner") if len(var_cubes) > 1 else var_cubes[0]
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            pieces.append(ds.sel(time=slice(time_range.start, time_range.end)))

        ds_all = xr.concat(pieces, dim="time").sortby("time") if len(pieces) > 1 else pieces[0]
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No AgERA5 data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"AgERA5 v{self._version} via CDS ({CDS_DATASET}); per-variable daily "
                "NetCDF cache, local bbox+time subset; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
        )
