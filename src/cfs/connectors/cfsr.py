# SPDX-License-Identifier: MIT
"""Historical NCEP CFSR hourly forcing from NCAR GDEX THREDDS."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

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
from cfs.subset.bbox import apply_bbox_subset, plan_bbox_subset
from cfs.subset.canonical import VariableMapping, harmonize

if TYPE_CHECKING:
    import xarray as xr


GDEX_ROOT = "https://tds.gdex.ucar.edu/thredds/dodsC/files/g/d093001"


@dataclass(frozen=True)
class _CFSRVar:
    token: str
    native: str
    canonical: CanonicalVar


_VARS = [
    _CFSRVar("tmp2m", "Temperature_height_above_ground", CanonicalVar.AIR_TEMPERATURE),
    _CFSRVar("q2m", "Specific_humidity_height_above_ground", CanonicalVar.SPECIFIC_HUMIDITY),
    _CFSRVar("pressfc", "Pressure_surface", CanonicalVar.SURFACE_AIR_PRESSURE),
    _CFSRVar("wnd10m", "u-component_of_wind_height_above_ground", CanonicalVar.EASTWARD_WIND),
    _CFSRVar("wnd10m", "v-component_of_wind_height_above_ground", CanonicalVar.NORTHWARD_WIND),
    _CFSRVar("prate", "Precipitation_rate_surface_Mixed_intervals_Average", CanonicalVar.PRECIPITATION_FLUX),
    _CFSRVar(
        "dswsfc",
        "Downward_Short-Wave_Radiation_Flux_surface_Mixed_intervals_Average",
        CanonicalVar.SHORTWAVE_RADIATION_DOWN,
    ),
    # "Radp" is the native GDEX/THREDDS spelling and must remain exact.
    _CFSRVar(
        "dlwsfc",
        "Downward_Long-Wave_Radp_Flux_surface_Mixed_intervals_Average",
        CanonicalVar.LONGWAVE_RADIATION_DOWN,
    ),
]
_MAPPINGS = [VariableMapping(v.native, v.canonical) for v in _VARS]


def _monthly_url(token: str, year: int, month: int) -> str:
    return f"{GDEX_ROOT}/{year}/{token}.gdas.{year}{month:02d}.grb2"


def _months(time_range: TimeRange) -> list[tuple[int, int]]:
    import pandas as pd

    return [(int(ts.year), int(ts.month)) for ts in pd.period_range(time_range.start, time_range.end, freq="M")]


def _open_opendap(url: str):
    try:
        import pydap  # noqa: F401
        import xarray as xr
    except ImportError as e:  # pragma: no cover
        raise MissingExtraError("CFSR OPeNDAP access needs the 'earthdata' extra (pydap)") from e
    return xr.open_dataset(url, engine="pydap")


@register("cfsr")
class CFSRConnector(BaseForcingConnector):
    slug = "cfsr"
    display_name = "NCEP CFSR selected hourly time-series (1979–2010)"
    base_url = GDEX_ROOT
    protocol = "opendap"

    async def list_products(self) -> list[ForcingProduct]:
        return [ForcingProduct(
            id="cfsr:hourly_timeseries",
            provider=self.slug,
            name="CFSR selected hourly surface forcing (T382, ~0.312°)",
            description=(
                "Historical companion to CFSv2/CDAS from NCAR GDEX d093001 "
                "monthly GRIB2 time-series exposed through THREDDS OPeNDAP."
            ),
            variables=[ProductVariable(canonical=m.canonical, source_name=m.source_name) for m in _MAPPINGS],
            resolution_deg=0.3125,
            bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
            temporal=TemporalExtent(
                start=datetime(1979, 1, 1),
                end=datetime(2010, 12, 31, 23),
                resolution=TemporalResolution.HOURLY,
            ),
            protocol=Protocol.OPENDAP,
            license="NCAR GDEX CC-BY-4.0",
            citation="Saha et al. (2010), CFSR, BAMS 91:1015–1057; NCAR GDEX d093001, DOI 10.5065/D6513W89.",
        )]

    async def fetch(self, product_id: str, bbox: BoundingBox, time_range: TimeRange,
                    variables: list[CanonicalVar] | None = None) -> tuple[xr.Dataset, FetchResult]:
        import xarray as xr

        t0 = time.monotonic()
        product = self._require_product(product_id, await self.list_products())
        settings = get_settings()
        self._guard_area(bbox, settings)
        if time_range.start.year < 1979 or time_range.end.year > 2010:
            raise SubsetError("CFSR d093001 coverage is 1979-01 through 2010-12; use cfsv2 for the recent companion")
        wanted = set(variables) if variables else None
        selected = [v for v in _VARS if wanted is None or v.canonical in wanted]
        if not selected:
            raise SubsetError("None of the requested variables are offered by CFSR")

        # Open each token only once: wnd10m carries both vector components.
        by_token: dict[str, list[_CFSRVar]] = {}
        for var in selected:
            by_token.setdefault(var.token, []).append(var)

        def _piece(token: str, token_vars: list[_CFSRVar], year: int, month: int):
            ds = _open_opendap(_monthly_url(token, year, month))
            names = [v.native for v in token_vars]
            ds = ds[names]
            plan = plan_bbox_subset(ds, bbox, lat_name="lat", lon_name="lon")
            ds = apply_bbox_subset(ds, plan, lat_name="lat", lon_name="lon")
            ds = ds.sel(time=slice(time_range.start, time_range.end))
            for dim in ("height_above_ground",):
                if ds.sizes.get(dim) == 1:
                    ds = ds.squeeze(dim, drop=True)
            return ds

        thunks = [
            partial(_piece, token, token_vars, year, month)
            for token, token_vars in by_token.items()
            for year, month in _months(time_range)
        ]
        pieces = await self._gather_pieces(thunks)
        pieces = [piece for piece in pieces if piece.sizes.get("time", 0)]
        if not pieces:
            raise SubsetError(f"No CFSR data in [{time_range.start}, {time_range.end}]")
        ds_all = xr.merge(pieces, join="inner")
        canonical = harmonize(ds_all, _MAPPINGS, requested=variables, lat_name="lat", lon_name="lon")
        return canonical, self._finalize(
            canonical, product=product, bbox=bbox, time_range=time_range,
            provenance="NCAR GDEX d093001 CFSR hourly time-series via THREDDS OPeNDAP; canonical-v1",
            t0=t0, settings=settings, lazy=True,
        )
