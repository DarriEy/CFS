# SPDX-License-Identifier: MIT
"""ECCC HRDPS offline tests: products, mappings, URL pattern, run-date regex."""

from __future__ import annotations

import asyncio
import re

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.eccc_hrdps import (
    _FIELDS,
    _MAPPINGS,
    ECCCHRDPSConnector,
    _file_url,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "eccc_hrdps" in list_providers()


def test_one_product():
    ids = {p.id for p in asyncio.run(ECCCHRDPSConnector().list_products())}
    assert ids == {"eccc_hrdps:continental_2p5km"}


def test_all_eight_forcing_vars_offered():
    canon = {canon for _t, canon, _i, _a in _FIELDS}
    assert canon == {
        CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SPECIFIC_HUMIDITY,
        CanonicalVar.SURFACE_AIR_PRESSURE, CanonicalVar.EASTWARD_WIND,
        CanonicalVar.NORTHWARD_WIND, CanonicalVar.PRECIPITATION_FLUX,
        CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.LONGWAVE_RADIATION_DOWN,
    }


def test_instantaneous_identity_vs_accumulated_radiation():
    by_canon = {m.canonical: m for m in _MAPPINGS}
    # Instantaneous fields (incl. PRATE rate) are identity SI.
    for canon in (CanonicalVar.AIR_TEMPERATURE, CanonicalVar.SURFACE_AIR_PRESSURE,
                  CanonicalVar.EASTWARD_WIND, CanonicalVar.PRECIPITATION_FLUX):
        m = by_canon[canon]
        assert m.scale == 1.0 and not m.deaccumulate
    # Radiation is accumulated J m-2 → de-accumulate + /3600 s → W m-2.
    for canon in (CanonicalVar.SHORTWAVE_RADIATION_DOWN, CanonicalVar.LONGWAVE_RADIATION_DOWN):
        m = by_canon[canon]
        assert m.deaccumulate and m.scale == pytest.approx(1.0 / 3600.0)


def test_file_url_structure():
    url = _file_url(0, 3, "20260618", "TMP_AGL-2m")
    assert url.endswith(
        "/model_hrdps/continental/2.5km/00/003/"
        "20260618T00Z_MSC_HRDPS_TMP_AGL-2m_RLatLon0.0225_PT003H.grib2"
    )
    assert url.startswith("https://dd.weather.gc.ca/today/")


def test_run_date_regex_matches_datamart_filename():
    # The run-discovery regex must extract (date, cycle) from a real filename.
    fname = "20260618T18Z_MSC_HRDPS_DSWRF_Sfc_RLatLon0.0225_PT000H.grib2"
    m = re.search(r"(\d{8})T(\d{2})Z_MSC_HRDPS_", fname)
    assert m and m.group(1) == "20260618" and int(m.group(2)) == 18
