# SPDX-License-Identifier: MIT
"""ECCC RDPS offline tests: products, mappings (inst/accum/bucket), URL, regex."""

from __future__ import annotations

import asyncio
import re

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.eccc_rdps import _FIELDS, _MAPPINGS, ECCCRDPSConnector, _file_url
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "eccc_rdps" in list_providers()


def test_one_product():
    ids = {p.id for p in asyncio.run(ECCCRDPSConnector().list_products())}
    assert ids == {"eccc_rdps:regional_10km"}


def test_all_eight_forcing_vars_offered():
    canon = {canon for _t, canon, _i, _m in _FIELDS}
    assert canon == {
        CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SPECIFIC_HUMIDITY,
        CanonicalVar.SURFACE_AIR_PRESSURE, CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND, CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.LONGWAVE_RADIATION_DOWN,
    }


def test_accumulation_modes():
    by_canon = {m.canonical: m for m in _MAPPINGS}
    # Instantaneous state fields are identity SI.
    for canon in (CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SURFACE_AIR_PRESSURE,
                  CanonicalVar.EASTWARD_WIND, CanonicalVar.SPECIFIC_HUMIDITY):
        assert by_canon[canon].scale == 1.0 and not by_canon[canon].deaccumulate
    # Radiation = run-total accumulation → de-accumulate + /3600.
    for canon in (CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.LONGWAVE_RADIATION_DOWN):
        assert by_canon[canon].deaccumulate and by_canon[canon].scale == pytest.approx(1.0 / 3600.0)
    # Precip = explicit 1-hour bucket → /3600, NO de-accumulation.
    precip = by_canon[CanonicalVar.PRECIPITATION_FLUX]
    assert precip.scale == pytest.approx(1.0 / 3600.0) and not precip.deaccumulate


def test_camelcase_tokens():
    # RDPS uses the new MSC camelCase dialect, not NCEP short names.
    tokens = {tok for tok, _c, _i, _m in _FIELDS}
    assert "AirTemp_AGL-2m" in tokens and "WindU_AGL-10m" in tokens
    assert "TMP_AGL-2m" not in tokens  # that is HRDPS's dialect


def test_file_url_structure():
    url = _file_url(0, 12, "20260618", "AirTemp_AGL-2m")
    assert url.endswith(
        "/model_rdps/10km/00/012/20260618T00Z_MSC_RDPS_AirTemp_AGL-2m_RLatLon0.09_PT012H.grib2"
    )


def test_run_date_regex():
    fname = "20260618T18Z_MSC_RDPS_Pressure_Sfc_RLatLon0.09_PT000H.grib2"
    m = re.search(r"(\d{8})T(\d{2})Z_MSC_RDPS_", fname)
    assert m and m.group(1) == "20260618" and int(m.group(2)) == 18
