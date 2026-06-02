# SPDX-License-Identifier: MIT
"""E-OBS offline tests: registration, products, mappings, zip extraction."""

from __future__ import annotations

import zipfile

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.eobs import _MAPPINGS, _VARS, _extract_ncs
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "eobs" in set(list_providers())


async def test_eobs_lists_both_grids():
    Conn = get_connector("eobs")
    async with Conn() as conn:
        ids = {p.id for p in await conn.list_products()}
    assert ids == {"eobs:ensemble_mean_0.1deg", "eobs:ensemble_mean_0.25deg"}


def test_eobs_exposes_only_clean_vars():
    # pp (sea-level pressure) and hu (relative humidity) are deliberately deferred.
    canon = {m.canonical for m in _MAPPINGS}
    assert canon == {
        CanonicalVar.AIR_TEMPERATURE,
        CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN,
        CanonicalVar.WIND_SPEED,
    }
    assert CanonicalVar.SURFACE_AIR_PRESSURE not in canon
    assert CanonicalVar.SPECIFIC_HUMIDITY not in canon
    assert {v.nc_name for v in _VARS} == {"tg", "rr", "qq", "fg"}


def test_eobs_version_override():
    Conn = get_connector("eobs")
    assert Conn()._version == "31_0e"  # CDS tokens use underscores
    assert Conn(config={"version": "30_0e"})._version == "30_0e"


def test_eobs_grid_tokens_use_underscores():
    # CDS request tokens are "0_1deg"/"0_25deg", not "0.1deg".
    conn = get_connector("eobs")()
    assert conn._grid_token("eobs:ensemble_mean_0.1deg") == "0_1deg"
    assert conn._grid_token("eobs:ensemble_mean_0.25deg") == "0_25deg"


def test_eobs_temperature_celsius_to_kelvin():
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {"tg": (("time", "latitude", "longitude"), np.full(shape, 15.0))},  # 15 °C
        coords={"time": [0], "latitude": [50.0, 50.1], "longitude": [5.0, 5.1]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="latitude", lon_name="longitude")
    assert float(out[CanonicalVar.AIR_TEMPERATURE].values.flat[0]) == pytest.approx(288.15)


def test_eobs_precip_mm_per_day_to_flux():
    shape = (1, 2, 2)
    ds = xr.Dataset(
        {"rr": (("time", "latitude", "longitude"), np.full(shape, 8.64))},  # mm/day
        coords={"time": [0], "latitude": [50.0, 50.1], "longitude": [5.0, 5.1]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="latitude", lon_name="longitude")
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(1e-4, rel=1e-6)  # 8.64 / 86400


def test_eobs_extract_ncs(tmp_path):
    # Build a fake CDS zip with a .nc member and confirm extraction is idempotent.
    nc = tmp_path / "tg_ens_mean_0.1deg_reg_v30.0e.nc"
    xr.Dataset({"tg": ("time", [1.0])}, coords={"time": [0]}).to_netcdf(nc)
    zpath = tmp_path / "eobs.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(nc, arcname=nc.name)
        zf.writestr("readme.txt", "ignore me")
    dest = tmp_path / "out"
    dest.mkdir()
    got = _extract_ncs(zpath, dest)
    assert len(got) == 1 and got[0].name.endswith(".nc")
    # Idempotent second call returns the same file without error.
    assert _extract_ncs(zpath, dest)[0].name == got[0].name
