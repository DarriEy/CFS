# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CERRA connector — Copernicus European Regional Reanalysis (via CDS).

~5.5 km European reanalysis. Unlike CARRA, CERRA provides a combined
``10m_wind_speed`` (mapped to canonical ``wind_speed``) rather than u/v
components, uses the standard −180..180 longitude convention, has no domain
split, and requires ``data_type: reanalysis`` in the request. Specific humidity
is derived from 2 m RH.

See :mod:`cfs.connectors.cds_reanalysis` for the shared analysis+forecast logic
and the caveat that the CDS short names / parameters are best-effort and should
be confirmed on the first authenticated fetch.
"""

from __future__ import annotations

from cfs.connectors.cds_reanalysis import CDSReanalysisConnector, _RVar
from cfs.core.models import BoundingBox
from cfs.core.registry import register
from cfs.core.vocabulary import CanonicalVar


@register("cerra")
class CERRAConnector(CDSReanalysisConnector):
    slug = "cerra"
    display_name = "CERRA — Copernicus European Regional Reanalysis (5.5 km, via CDS)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    dataset = "reanalysis-cerra-single-levels"
    grid = [0.05, 0.05]
    domain = None
    extra_request = {"level_type": "surface_or_atmosphere", "data_type": "reanalysis"}
    resolution_deg = 0.05
    product_bbox = BoundingBox(min_lon=-58, min_lat=20, max_lon=74, max_lat=74)
    citation = "Copernicus Climate Change Service (C3S), CERRA — European Regional Reanalysis."

    analysis_vars = [
        _RVar("2m_temperature", "t2m", CanonicalVar.AIR_TEMPERATURE),
        _RVar("10m_wind_speed", "si10", CanonicalVar.WIND_SPEED),
        _RVar("surface_pressure", "sp", CanonicalVar.SURFACE_AIR_PRESSURE),
    ]
    forecast_vars = [
        _RVar("total_precipitation", "tp", CanonicalVar.PRECIPITATION_FLUX,
              scale=1.0 / 3600.0, note="CERRA 1-h forecast accumulation (kg m-2) -> flux"),
        _RVar("surface_solar_radiation_downwards", "ssrd", CanonicalVar.SHORTWAVE_RADIATION_DOWN,
              scale=1.0 / 3600.0, note="CERRA 1-h forecast accumulation (J m-2) -> flux"),
        _RVar("surface_thermal_radiation_downwards", "strd", CanonicalVar.LONGWAVE_RADIATION_DOWN,
              scale=1.0 / 3600.0, note="CERRA 1-h forecast accumulation (J m-2) -> flux"),
    ]
    rh_name, t_name, p_name = "r2", "t2m", "sp"
