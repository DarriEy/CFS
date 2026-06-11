# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""E-OBS connector — European gauge-based gridded observations via the CDS.

E-OBS is the ensemble-mean daily gridded land-surface *observational* analysis for
Europe (ECA&D station network interpolated to a 0.1°/0.25° grid, 1950→present),
retrieved from the Copernicus CDS (``insitu-gridded-observations-europe``). It
fills the European observational gap — CFS otherwise has only reanalysis there
(CARRA/CERRA).

Unlike the reanalysis CDS connectors (ERA5-Land/CARRA/CERRA), the E-OBS dataset
has **no server-side ``area`` subset**: a request returns a ZIP holding one
full-domain NetCDF per variable for the whole ``full_period`` series. CFS
therefore downloads the (large, cached) per-variable file once and subsets to the
bbox + time range *locally* — xarray opens it lazily so only the requested
hyperslab is read into memory, and the cached download is reused across any later
bbox/time request.

Variable scope (deliberately conservative, matching the project's "don't ship an
unverified conversion" stance):
  * exposed: ``tg``→air_temperature (°C→K), ``rr``→precipitation_flux
    (mm/day→kg m⁻² s⁻¹), ``qq``→shortwave_down (W m⁻², identity), ``fg``→wind_speed
    (m s⁻¹, identity; E-OBS wind starts 1980).
  * **deferred**: ``pp`` is *sea-level* pressure, not surface pressure (wrong over
    terrain), and ``hu`` (relative humidity) can't be turned into specific humidity
    without a *surface* pressure E-OBS doesn't provide. Both are omitted rather
    than shipped as a questionable mapping.

Auth-gated: needs CDS credentials (``~/.cdsapirc`` or ``CDSAPI_URL``/``CDSAPI_KEY``)
and acceptance of the E-OBS licence on the CDS site. Request tokens
(``grid_resolution`` ``0_1deg``/``0_25deg``, ``version`` ``31_0e``, ``period``
``full_period``) were checked against the live CDS form constraints, and a live
retrieve confirmed credentials + request validate server-side; the retrieve then
returns only once the E-OBS dataset licence has been accepted on the CDS site (a
one-time manual step). The version is overridable via ``config={"version": "30_0e"}``.
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

CDS_DATASET = "insitu-gridded-observations-europe"
# CDS version token uses underscores (e.g. "31_0e" = v31.0e); override via config.
_DEFAULT_VERSION = "31_0e"

# E-OBS approximate domain (ECA&D land grid): ~25°W–45°E, 25°N–71.5°N.
_DOMAIN = BoundingBox(min_lon=-25.0, min_lat=25.0, max_lon=45.0, max_lat=71.5)

# Product-id grid key → (CDS request token, resolution in degrees). The CDS token
# uses underscores ("0_1deg"), confirmed against the dataset form constraints.
_GRIDS = {"0.1deg": ("0_1deg", 0.1), "0.25deg": ("0_25deg", 0.25)}


@dataclass(frozen=True)
class _EobsVar:
    """An E-OBS variable: CDS request name, NetCDF short name, mapping."""

    request_name: str  # CDS request 'variable' token
    nc_name: str       # variable name inside the downloaded NetCDF
    canonical: CanonicalVar
    scale: float = 1.0
    offset: float = 0.0
    note: str = ""


_VARS: list[_EobsVar] = [
    _EobsVar("mean_temperature", "tg", CanonicalVar.AIR_TEMPERATURE,
             offset=273.15, note="E-OBS mean temperature (°C) -> K"),
    _EobsVar("precipitation_amount", "rr", CanonicalVar.PRECIPITATION_FLUX,
             scale=1.0 / 86400.0, note="E-OBS daily precip (mm/day) -> flux (kg m-2 s-1)"),
    _EobsVar("surface_shortwave_downwelling_radiation", "qq",
             CanonicalVar.SHORTWAVE_RADIATION_DOWN, note="E-OBS global radiation (W m-2)"),
    _EobsVar("wind_speed", "fg", CanonicalVar.WIND_SPEED,
             note="E-OBS mean wind speed (m s-1); available from 1980"),
]


def _mappings_for(selected: list[_EobsVar]) -> list[VariableMapping]:
    return [
        VariableMapping(v.nc_name, v.canonical, scale=v.scale, offset=v.offset, note=v.note)
        for v in selected
    ]


# Full mapping table (NetCDF short names → canonical), exposed for tests.
_MAPPINGS: list[VariableMapping] = _mappings_for(_VARS)


def _extract_ncs(zip_path: Path, dest: Path) -> list[Path]:
    """Extract every .nc member of a CDS E-OBS zip into ``dest`` (idempotent)."""
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.lower().endswith(".nc"):
                target = dest / Path(member).name
                if not (target.exists() and target.stat().st_size > 0):
                    with zf.open(member) as src, open(target, "wb") as fh:
                        fh.write(src.read())
                out.append(target)
    return out


@register("eobs")
class EOBSConnector(CDSAPIMixin, BaseForcingConnector):
    slug = "eobs"
    display_name = "E-OBS European gridded observations (0.1°/0.25° daily, via CDS)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    @property
    def _version(self) -> str:
        return str(self.config.get("version", _DEFAULT_VERSION))

    async def list_products(self) -> list[ForcingProduct]:
        products = []
        for key, (_token, res_deg) in _GRIDS.items():
            products.append(
                ForcingProduct(
                    id=f"{self.slug}:ensemble_mean_{key}",
                    provider=self.slug,
                    name=f"E-OBS ensemble-mean daily ({key})",
                    description=(
                        "E-OBS ensemble-mean daily gridded observations for Europe "
                        "(ECA&D stations interpolated), retrieved from the Copernicus "
                        "CDS. Full-domain download, subset to bbox + time locally."
                    ),
                    variables=[
                        ProductVariable(canonical=v.canonical, source_name=v.nc_name)
                        for v in _VARS
                    ],
                    resolution_deg=res_deg,
                    crs="EPSG:4326",
                    bbox=_DOMAIN,
                    temporal=TemporalExtent(
                        start=None, resolution=TemporalResolution.DAILY,
                    ),
                    protocol=Protocol.REST,
                    license="Copernicus / ECA&D E-OBS licence (free, attribution; accept on CDS).",
                    citation="Cornes et al. (2018), E-OBS v17, JGR Atmospheres 123:9391-9409.",
                )
            )
        return products

    def _grid_token(self, product_id: str) -> str:
        key = product_id.split(":", 1)[1].replace("ensemble_mean_", "")
        if key not in _GRIDS:
            raise SubsetError(f"Unknown E-OBS grid '{key}'")
        return _GRIDS[key][0]

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

        grid = self._grid_token(product_id)
        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by E-OBS")

        cache = self._cds_cache_dir()
        extract_dir = cache / f"extracted_{grid}_v{self._version}"
        extract_dir.mkdir(parents=True, exist_ok=True)

        # One full_period download per variable (each a separate file in the CDS
        # zip) → cached and reused across every later bbox/time request.
        pieces = []
        for v in selected:
            request = {
                "product_type": "ensemble_mean",
                "variable": v.request_name,
                "grid_resolution": grid,
                "period": "full_period",
                "version": self._version,
                "download_format": "zip",
            }
            zip_target = cache / f"{self.slug}_{v.nc_name}_{grid}_v{self._version}.zip"
            await self._cds_retrieve(CDS_DATASET, request, zip_target)
            ncs = _extract_ncs(zip_target, extract_dir)
            if not ncs:
                raise SubsetError(f"E-OBS zip for '{v.request_name}' held no .nc file")
            ds = xr.open_dataset(ncs[0])
            if v.nc_name not in ds.data_vars:
                # Fall back to the single data variable if the short name differs.
                only = [d for d in ds.data_vars]
                if len(only) == 1:
                    ds = ds.rename({only[0]: v.nc_name})
                else:
                    raise SubsetError(
                        f"E-OBS file for '{v.request_name}' lacks variable '{v.nc_name}' "
                        f"(found {only})"
                    )
            plan = plan_bbox_subset(ds, bbox, lat_name="latitude", lon_name="longitude")
            ds = apply_bbox_subset(ds, plan, lat_name="latitude", lon_name="longitude")
            ds = ds[[v.nc_name]].sel(time=slice(time_range.start, time_range.end))
            pieces.append(ds)

        ds_all = xr.merge(pieces, join="inner") if len(pieces) > 1 else pieces[0]
        if ds_all.sizes.get("time", 0) == 0:
            raise SubsetError(f"No E-OBS data in [{time_range.start}, {time_range.end}]")

        canonical = harmonize(
            ds_all, _mappings_for(selected), requested=variables,
            lat_name="latitude", lon_name="longitude",
        )
        return canonical, self._finalize(
            canonical,
            product=product,
            bbox=bbox,
            time_range=time_range,
            provenance=(
                f"E-OBS v{self._version} ensemble-mean ({grid}) via CDS ({CDS_DATASET}); "
                "full-domain download, local bbox+time subset; canonical-v1"
            ),
            t0=t0,
            settings=settings,
            lazy=False,
            extra_warnings=[
                "E-OBS has no server-side area subset: the full European domain is "
                "downloaded once per variable (large, cached) and subset locally."
            ],
        )
