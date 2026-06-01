# SPDX-License-Identifier: MIT
"""Harmonization tests — the canonical boundary, no network needed."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.era5_arco import _MAPPINGS
from cfs.core.exceptions import HarmonizationError
from cfs.core.vocabulary import CANONICAL, CanonicalVar
from cfs.subset.canonical import harmonize


def _fake_era5_cube() -> xr.Dataset:
    """A tiny ERA5-like cube with native names and units."""
    time = np.arange(3)
    lat = np.array([51.0, 50.75, 50.5])
    lon = np.array([245.0, 245.25])  # 0-360 convention
    shape = (3, 3, 2)
    return xr.Dataset(
        {
            "2m_temperature": (("time", "latitude", "longitude"), np.full(shape, 280.0)),
            "total_precipitation": (("time", "latitude", "longitude"), np.full(shape, 0.0036)),  # m/hr
            "surface_solar_radiation_downwards": (("time", "latitude", "longitude"), np.full(shape, 3600.0)),  # J/m2
            "surface_pressure": (("time", "latitude", "longitude"), np.full(shape, 90000.0)),
        },
        coords={"time": time, "latitude": lat, "longitude": lon},
    )


def test_renames_to_canonical_names():
    out = harmonize(_fake_era5_cube(), _MAPPINGS)
    assert CanonicalVar.AIR_TEMPERATURE in out.data_vars
    assert CanonicalVar.PRECIPITATION_FLUX in out.data_vars
    assert "2m_temperature" not in out.data_vars  # native name gone


def test_units_attached_from_vocabulary():
    out = harmonize(_fake_era5_cube(), _MAPPINGS)
    assert out[CanonicalVar.AIR_TEMPERATURE].attrs["units"] == "K"
    assert out[CanonicalVar.PRECIPITATION_FLUX].attrs["units"] == "kg m-2 s-1"
    assert out.attrs["cfs_schema"] == "canonical-v1"


def test_precip_accumulation_to_flux_conversion():
    # 0.0036 m/hr * 1000 kg/m3 / 3600 s = 0.001 kg m-2 s-1
    out = harmonize(_fake_era5_cube(), _MAPPINGS)
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(0.001, rel=1e-6)


def test_radiation_energy_to_flux_conversion():
    # 3600 J/m2 over 1 h / 3600 s = 1.0 W m-2
    out = harmonize(_fake_era5_cube(), _MAPPINGS)
    val = float(out[CanonicalVar.SHORTWAVE_RADIATION_DOWN].values.flat[0])
    assert val == pytest.approx(1.0, rel=1e-6)


def test_temperature_is_identity():
    out = harmonize(_fake_era5_cube(), _MAPPINGS)
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(280.0)


def test_requested_subset_only():
    out = harmonize(_fake_era5_cube(), _MAPPINGS, requested=[CanonicalVar.AIR_TEMPERATURE])
    assert set(out.data_vars) == {CanonicalVar.AIR_TEMPERATURE}


def test_no_match_raises():
    with pytest.raises(HarmonizationError):
        harmonize(_fake_era5_cube(), _MAPPINGS, requested=[CanonicalVar.SPECIFIC_HUMIDITY])


def test_converted_values_within_canonical_valid_range():
    out = harmonize(_fake_era5_cube(), _MAPPINGS)
    for var in out.data_vars:
        spec = CANONICAL[CanonicalVar(str(var))]
        if spec.valid_range:
            lo, hi = spec.valid_range
            v = float(out[var].values.flat[0])
            assert lo <= v <= hi, f"{var}={v} outside {spec.valid_range}"
