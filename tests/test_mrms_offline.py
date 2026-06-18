# SPDX-License-Identifier: MIT
"""MRMS offline tests: product, precip->flux mapping, key parsing/selection."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.mrms import (
    _ACCUM_SECONDS,
    _MAPPINGS,
    MRMSConnector,
    _nearest_key,
    _object_url,
    _parse_key_time,
    _prefix,
)
from cfs.core.registry import discover, list_providers
from cfs.core.vocabulary import CanonicalVar


def test_registered():
    discover()
    assert "mrms" in list_providers()


def test_one_precip_only_product():
    ids = {p.id for p in asyncio.run(MRMSConnector().list_products())}
    assert ids == {"mrms:multisensor_qpe_01h"}
    # The only canonical variable offered is precipitation_flux.
    prod = asyncio.run(MRMSConnector().list_products())[0]
    assert {v.canonical for v in prod.variables} == {CanonicalVar.PRECIPITATION_FLUX}


def test_precip_flux_mapping_divides_by_window():
    assert len(_MAPPINGS) == 1
    m = _MAPPINGS[0]
    assert m.canonical == CanonicalVar.PRECIPITATION_FLUX
    assert m.scale == pytest.approx(1.0 / _ACCUM_SECONDS)  # mm over 1 h -> kg m-2 s-1
    assert not m.deaccumulate


def test_prefix_and_object_url():
    assert _prefix(datetime(2026, 6, 17)) == "CONUS/MultiSensor_QPE_01H_Pass2_00.00/20260617/"
    key = "CONUS/MultiSensor_QPE_01H_Pass2_00.00/20260617/MRMS_MultiSensor_QPE_01H_Pass2_00.00_20260617-120000.grib2.gz"
    assert _object_url(key) == f"https://noaa-mrms-pds.s3.amazonaws.com/{key}"


def test_parse_key_time():
    key = "CONUS/MultiSensor_QPE_01H_Pass2_00.00/20260617/MRMS_MultiSensor_QPE_01H_Pass2_00.00_20260617-130000.grib2.gz"
    assert _parse_key_time(key) == datetime(2026, 6, 17, 13, 0, 0)
    assert _parse_key_time("CONUS/.../not-a-real-name.txt") is None


def test_nearest_key_within_and_beyond_tolerance():
    keyed = [
        (datetime(2026, 6, 17, 12), "k12"),
        (datetime(2026, 6, 17, 13), "k13"),
        (datetime(2026, 6, 17, 14), "k14"),
    ]
    # Exact hour picks that file.
    assert _nearest_key(keyed, datetime(2026, 6, 17, 13)) == "k13"
    # 20 min off still within the 30 min tolerance.
    assert _nearest_key(keyed, datetime(2026, 6, 17, 13, 20)) == "k13"
    # A target far from any file (within retained span but no nearby object).
    assert _nearest_key(keyed, datetime(2026, 6, 17, 18)) is None
