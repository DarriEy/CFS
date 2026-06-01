# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CARRA connector — Copernicus Arctic Regional Reanalysis (via CDS).

~2.5 km Arctic reanalysis. Wind is provided as u/v components; specific humidity
is derived from 2 m RH. The Arctic domain is split into ``west_domain``
(Greenland / Iceland / NE Canada — the default here) and ``east_domain``
(Scandinavia / Svalbard); override via ``config={'domain': 'east_domain'}``.

See :mod:`cfs.connectors.cds_reanalysis` for the shared analysis+forecast logic
and the caveat that the CDS short names / parameters are best-effort and should
be confirmed on the first authenticated fetch.
"""

from __future__ import annotations

from cfs.connectors.cds_reanalysis import CDSReanalysisConnector, _RVar
from cfs.core.models import BoundingBox
from cfs.core.registry import register
from cfs.core.vocabulary import CanonicalVar


@register("carra")
class CARRAConnector(CDSReanalysisConnector):
    slug = "carra"
    display_name = "CARRA — Copernicus Arctic Regional Reanalysis (2.5 km, via CDS)"
    base_url = "https://cds.climate.copernicus.eu/api"
    protocol = "cds_api"

    dataset = "reanalysis-carra-single-levels"
    grid = [0.025, 0.025]
    extra_request = {"level_type": "surface_or_atmosphere"}
    resolution_deg = 0.025
    product_bbox = BoundingBox(min_lon=-180, min_lat=55, max_lon=180, max_lat=90)
    citation = "Copernicus Climate Change Service (C3S), CARRA — Arctic Regional Reanalysis."

    analysis_vars = [
        _RVar("2m_temperature", "t2m", CanonicalVar.AIR_TEMPERATURE),
        _RVar("10m_u_component_of_wind", "u10", CanonicalVar.EASTWARD_WIND),
        _RVar("10m_v_component_of_wind", "v10", CanonicalVar.NORTHWARD_WIND),
        _RVar("surface_pressure", "sp", CanonicalVar.SURFACE_AIR_PRESSURE),
    ]
    # Forecast accumulations are over a single 1-hour leadtime → linear /3600.
    forecast_vars = [
        _RVar("total_precipitation", "tp", CanonicalVar.PRECIPITATION_FLUX,
              scale=1.0 / 3600.0, note="CARRA 1-h forecast accumulation (kg m-2) -> flux"),
        _RVar("surface_solar_radiation_downwards", "ssrd", CanonicalVar.SHORTWAVE_RADIATION_DOWN,
              scale=1.0 / 3600.0, note="CARRA 1-h forecast accumulation (J m-2) -> flux"),
        _RVar("thermal_surface_radiation_downwards", "strd", CanonicalVar.LONGWAVE_RADIATION_DOWN,
              scale=1.0 / 3600.0, note="CARRA 1-h forecast accumulation (J m-2) -> flux"),
    ]
    rh_name, t_name, p_name = "r2", "t2m", "sp"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # west_domain (Greenland/Iceland/NE Canada) by default; override via config.
        self.domain = (self.config or {}).get("domain", "west_domain")
