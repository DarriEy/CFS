# SPDX-License-Identifier: MIT
"""PERSIANN-CDR offline tests: registration, year-index URL/filename parsing, conversion."""

from __future__ import annotations

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

from cfs.connectors.persiann_cdr import _MAPPINGS, _parse_year_index
from cfs.core.models import TimeRange
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar
from cfs.subset.canonical import harmonize


def test_registered():
    discover()
    assert "persiann_cdr" in list_providers()
    conn_cls = get_connector("persiann_cdr")
    assert conn_cls.slug == "persiann_cdr"


def test_year_index_resolves_creation_suffix_filename():
    # The trailing _c{creation-date} token varies per file, so the connector must
    # map each YYYYMMDD to the exact published filename from the year index.
    html = (
        '<a href="PERSIANN-CDR_v01r01_20160601_c20161115.nc">x</a>'
        '<a href="PERSIANN-CDR_v01r01_20160602_c20161115.nc">x</a>'
        '<a href="PERSIANN-CDR_v01r01_20160603_c20170101.nc">x</a>'
    )
    index = _parse_year_index(html)
    assert index["20160601"] == "PERSIANN-CDR_v01r01_20160601_c20161115.nc"
    assert index["20160603"] == "PERSIANN-CDR_v01r01_20160603_c20170101.nc"
    assert len(index) == 3


def test_days_span_inclusive():
    from datetime import datetime

    from cfs.connectors.persiann_cdr import _days

    tr = TimeRange(start=datetime(2016, 6, 1), end=datetime(2016, 6, 3))
    assert _days(tr) == ["20160601", "20160602", "20160603"]


def test_daily_mm_to_flux():
    # PERSIANN files are dimensioned (time, lon, lat); 8.64 mm/day -> 1e-4 kg m-2 s-1.
    ds = xr.Dataset(
        {"precipitation": (("time", "lon", "lat"), np.full((1, 2, 2), 8.64))},  # mm/day
        coords={"time": [0], "lon": [100.0, 100.25], "lat": [-9.0, -8.75]},
    )
    out = harmonize(ds, _MAPPINGS, lat_name="lat", lon_name="lon")
    out = out.transpose("time", "latitude", "longitude", ...)
    val = float(out[CanonicalVar.PRECIPITATION_FLUX].values.flat[0])
    assert val == pytest.approx(1e-4, rel=1e-6)  # 8.64 / 86400
    assert out[CanonicalVar.PRECIPITATION_FLUX].dims == ("time", "latitude", "longitude")
