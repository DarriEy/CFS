"""Focused tests for host-independent SYMFLUENCE helpers."""

import numpy as np
import xarray as xr

from cfs.integrations._symfluence_helpers import (
    add_native_era5_derivations,
    as_list,
    config_value,
    derive_wind_speed,
    netcdf_encoding,
    product_tag,
)


def test_scalar_helpers_cover_config_and_file_naming():
    assert product_tag("NEX-GDDP: SSP2 / Model") == "nex_gddp_ssp2_model"
    assert as_list(" tas, pr, , huss ") == ["tas", "pr", "huss"]
    assert as_list([]) is None
    assert config_value({"SET": "value", "NONE": None}, "SET") == "value"
    assert config_value({"NONE": None}, "NONE", "fallback") == "fallback"
    assert config_value(object(), "MISSING", 3) == 3


def test_netcdf_encoding_covers_only_data_variables():
    ds = xr.Dataset({"air_temperature": ("time", [273.0])}, coords={"time": [0]})
    assert netcdf_encoding(ds) == {"air_temperature": {"zlib": True, "complevel": 1}}


def test_wind_speed_derivation_is_float32_and_idempotent():
    ds = xr.Dataset({
        "eastward_wind": ("time", np.array([3.0], dtype="float32")),
        "northward_wind": ("time", np.array([4.0], dtype="float32")),
    })
    result = derive_wind_speed(ds)
    assert result["wind_speed"].dtype == np.dtype("float32")
    assert result["wind_speed"].item() == 5.0
    assert derive_wind_speed(result) is result


def test_era5_derivations_add_expected_variables_and_preserve_existing():
    ds = xr.Dataset({
        "eastward_wind": ("time", np.array([3.0], dtype="float32")),
        "northward_wind": ("time", np.array([4.0], dtype="float32")),
        "dewpoint_temperature": ("time", [283.15]),
        "surface_air_pressure": ("time", [100_000.0]),
    })
    result = add_native_era5_derivations(ds)
    assert {"wind_speed", "specific_humidity"} <= set(result.data_vars)
    assert result["specific_humidity"].dtype == np.dtype("float32")

    existing = xr.Dataset({"wind_speed": ("time", [9.0]), "specific_humidity": ("time", [0.2])})
    assert add_native_era5_derivations(existing) is existing
    assert existing["wind_speed"].item() == 9.0
    assert existing["specific_humidity"].item() == 0.2
