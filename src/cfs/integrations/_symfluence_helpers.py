# SPDX-License-Identifier: MIT
"""Host-independent helpers for the CFS SYMFLUENCE integration."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import xarray as xr


def netcdf_encoding(ds: xr.Dataset) -> dict[str, dict[str, Any]]:
    """Return compressed per-variable encoding for Dataset.to_netcdf."""
    return {str(name): {"zlib": True, "complevel": 1} for name in ds.data_vars}


def product_tag(product: str) -> str:
    """Return a filesystem-safe tag for a CFS product identifier."""
    return re.sub(r"[^A-Za-z0-9]+", "_", product).strip("_").lower()


def config_value(config: Any, key: str, default: Any = None) -> Any:
    """Read flat config from a dict or SymfluenceConfig-like object."""
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        value = getter(key, default)
        return default if value is None else value
    return default


def as_list(value: Any) -> list[str] | None:
    """Coerce a comma-separated string or iterable into a list of names."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
        return items or None
    return [str(item) for item in value] or None


def derive_wind_speed(ds: xr.Dataset) -> xr.Dataset:
    """Derive wind_speed from wind primitives when absent."""
    if "wind_speed" in ds.data_vars or not (
        {"eastward_wind", "northward_wind"} <= set(ds.data_vars)
    ):
        return ds
    u, v = ds["eastward_wind"], ds["northward_wind"]
    wind = ((u**2 + v**2) ** 0.5).astype("float32")
    wind.attrs = {"units": "m s-1", "long_name": "wind speed", "standard_name": "wind_speed"}
    ds["wind_speed"] = wind
    return ds


def add_native_era5_derivations(ds: xr.Dataset) -> xr.Dataset:
    """Derive wind speed and humidity using native SYMFLUENCE op order."""
    import numpy as np
    import xarray

    ds = derive_wind_speed(ds)
    if "specific_humidity" not in ds.data_vars and {
        "dewpoint_temperature",
        "surface_air_pressure",
    } <= set(ds.data_vars):
        td_c = ds["dewpoint_temperature"] - 273.15
        es = 611.2 * np.exp((17.67 * td_c) / (td_c + 243.5))
        pressure = ds["surface_air_pressure"]
        denom = xarray.where((pressure - es) <= 1.0, 1.0, pressure - es)
        r = 0.622 * es / denom
        q = (r / (1.0 + r)).astype("float32")
        q.attrs = {"units": "kg kg-1", "long_name": "specific humidity", "standard_name": "specific_humidity"}
        ds["specific_humidity"] = q
    return ds
