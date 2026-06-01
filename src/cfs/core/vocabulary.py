# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""The CFS canonical forcing vocabulary — the schema CFS harmonizes *to*.

This is the contract that makes CFS reusable: every connector renames its native
variables to these canonical names and converts to these canonical (SI, CF-style)
units. The result is provider-agnostic, so a consumer can swap ERA5 for RDRS or
AORC without touching downstream code.

Deliberately *not* here: model-specific names (SUMMA's ``airtemp``/``pptrate``,
FUSE's conventions). That renaming is the consumer's final step, not CFS's. CFS
stops at the canonical dataset.

Canonical units follow CF conventions:
  * temperatures in kelvin (K)
  * precipitation as a mass flux (kg m-2 s-1), i.e. a *rate*, never an accumulation
  * radiation as a flux density (W m-2), i.e. a *rate*, never an accumulation
  * winds as vector components (m s-1)
  * pressure in pascals (Pa)
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class CanonicalVar(StrEnum):
    """Canonical forcing variable names (CF-aligned standard names)."""

    AIR_TEMPERATURE = "air_temperature"
    DEWPOINT_TEMPERATURE = "dewpoint_temperature"
    SPECIFIC_HUMIDITY = "specific_humidity"
    PRECIPITATION_FLUX = "precipitation_flux"
    EASTWARD_WIND = "eastward_wind"
    NORTHWARD_WIND = "northward_wind"
    WIND_SPEED = "wind_speed"
    SURFACE_AIR_PRESSURE = "surface_air_pressure"
    SHORTWAVE_RADIATION_DOWN = "surface_downwelling_shortwave_flux"
    LONGWAVE_RADIATION_DOWN = "surface_downwelling_longwave_flux"


class CanonicalSpec(BaseModel):
    """The canonical definition of one forcing variable."""

    name: CanonicalVar
    units: str
    description: str
    valid_range: tuple[float, float] | None = None


# Single source of truth for the canonical schema.
CANONICAL: dict[CanonicalVar, CanonicalSpec] = {
    CanonicalVar.AIR_TEMPERATURE: CanonicalSpec(
        name=CanonicalVar.AIR_TEMPERATURE,
        units="K",
        description="Near-surface (2 m) air temperature",
        valid_range=(180.0, 340.0),
    ),
    CanonicalVar.DEWPOINT_TEMPERATURE: CanonicalSpec(
        name=CanonicalVar.DEWPOINT_TEMPERATURE,
        units="K",
        description="Near-surface (2 m) dewpoint temperature",
        valid_range=(180.0, 320.0),
    ),
    CanonicalVar.SPECIFIC_HUMIDITY: CanonicalSpec(
        name=CanonicalVar.SPECIFIC_HUMIDITY,
        units="kg kg-1",
        description="Near-surface specific humidity",
        valid_range=(0.0, 0.1),
    ),
    CanonicalVar.PRECIPITATION_FLUX: CanonicalSpec(
        name=CanonicalVar.PRECIPITATION_FLUX,
        units="kg m-2 s-1",
        description="Precipitation rate (rain + snow water equivalent)",
        valid_range=(0.0, 0.1),
    ),
    CanonicalVar.EASTWARD_WIND: CanonicalSpec(
        name=CanonicalVar.EASTWARD_WIND,
        units="m s-1",
        description="Eastward (u) wind component at 10 m",
        valid_range=(-150.0, 150.0),
    ),
    CanonicalVar.NORTHWARD_WIND: CanonicalSpec(
        name=CanonicalVar.NORTHWARD_WIND,
        units="m s-1",
        description="Northward (v) wind component at 10 m",
        valid_range=(-150.0, 150.0),
    ),
    CanonicalVar.WIND_SPEED: CanonicalSpec(
        name=CanonicalVar.WIND_SPEED,
        units="m s-1",
        description="Scalar wind speed at 10 m",
        valid_range=(0.0, 150.0),
    ),
    CanonicalVar.SURFACE_AIR_PRESSURE: CanonicalSpec(
        name=CanonicalVar.SURFACE_AIR_PRESSURE,
        units="Pa",
        description="Surface air pressure",
        valid_range=(40000.0, 110000.0),
    ),
    CanonicalVar.SHORTWAVE_RADIATION_DOWN: CanonicalSpec(
        name=CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        units="W m-2",
        description="Surface downwelling shortwave radiation flux",
        valid_range=(0.0, 1500.0),
    ),
    CanonicalVar.LONGWAVE_RADIATION_DOWN: CanonicalSpec(
        name=CanonicalVar.LONGWAVE_RADIATION_DOWN,
        units="W m-2",
        description="Surface downwelling longwave radiation flux",
        valid_range=(0.0, 750.0),
    ),
}
