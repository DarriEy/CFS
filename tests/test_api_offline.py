# SPDX-License-Identifier: MIT
"""Offline tests for the blessed public facade: import surface, one-shot
``cfs.fetch()`` / ``cfs.fetch_sync()`` (via fake registered connectors — no
network), and ``cfs.configure()``."""

from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pytest

xr = pytest.importorskip("xarray")

import cfs
from cfs.connectors.base import BaseForcingConnector
from cfs.core.config import get_settings
from cfs.core.models import (
    BoundingBox,
    FetchResult,
    ForcingProduct,
    ProductVariable,
    Protocol,
    TimeRange,
)
from cfs.core.registry import _REGISTRY, register
from cfs.core.vocabulary import CanonicalVar

BOX = BoundingBox(min_lon=-1.0, min_lat=49.0, max_lon=1.0, max_lat=51.0)
TR = TimeRange(start=datetime(2020, 1, 1), end=datetime(2020, 1, 2))


# ── Fake connectors (synthetic canonical cube, no network) ──────────


def _product(provider: str, product: str) -> ForcingProduct:
    return ForcingProduct(
        id=f"{provider}:{product}",
        provider=provider,
        name=f"Fake {product}",
        variables=[ProductVariable(canonical=CanonicalVar.AIR_TEMPERATURE, source_name="t2m")],
        resolution_deg=0.1,
        bbox=BoundingBox(min_lon=-180, min_lat=-90, max_lon=180, max_lat=90),
        protocol=Protocol.ZARR,
    )


class _FakeBase(BaseForcingConnector):
    display_name = "Fake"
    base_url = ""
    protocol = "zarr"
    products: tuple[str, ...] = ("demo",)

    async def list_products(self):
        return [_product(self.slug, p) for p in self.products]

    async def fetch(self, product_id, bbox, time_range, variables=None):
        assert variables is None or all(isinstance(v, CanonicalVar) for v in variables)
        ds = xr.Dataset(
            {
                CanonicalVar.AIR_TEMPERATURE.value: (
                    ("time", "latitude", "longitude"),
                    np.full((2, 2, 2), 280.0),
                )
            },
            coords={"time": [0, 1], "latitude": [50.0, 50.1], "longitude": [0.0, 0.1]},
        )
        result = FetchResult(
            product_id=product_id,
            provider=self.slug,
            variables=[CanonicalVar.AIR_TEMPERATURE],
            bbox=bbox,
            time_range=time_range,
            n_times=2,
            n_lat=2,
            n_lon=2,
            resolution_deg=0.1,
            lazy=False,
            provenance=f"fake config={self.config} variables={variables}",
        )
        return ds, result


class _FakeOne(_FakeBase):
    slug = "fake_one"
    products = ("demo",)


class _FakeTwo(_FakeBase):
    slug = "fake_two"
    products = ("alpha", "beta")


@pytest.fixture
def fake_providers():
    register("fake_one")(_FakeOne)
    register("fake_two")(_FakeTwo)
    yield
    _REGISTRY.pop("fake_one", None)
    _REGISTRY.pop("fake_two", None)


# ── Import surface ──────────────────────────────────────────────────


def test_facade_exports():
    expected = {
        "BoundingBox",
        "CanonicalVar",
        "FetchResult",
        "TimeRange",
        "__version__",
        "configure",
        "discover",
        "fetch",
        "fetch_sync",
        "get_connector",
        "list_providers",
    }
    assert set(cfs.__all__) == expected
    for name in cfs.__all__:
        assert getattr(cfs, name) is not None


def test_facade_names_are_the_real_objects():
    from cfs.core import models, registry, vocabulary

    assert cfs.discover is registry.discover
    assert cfs.get_connector is registry.get_connector
    assert cfs.list_providers is registry.list_providers
    assert cfs.BoundingBox is models.BoundingBox
    assert cfs.TimeRange is models.TimeRange
    assert cfs.FetchResult is models.FetchResult
    assert cfs.CanonicalVar is vocabulary.CanonicalVar


def test_version():
    assert cfs.__version__ == "0.4.1"


def test_importing_cfs_does_not_import_connectors():
    # Connector modules must stay lazy: only discover() imports them. A fresh
    # interpreter proves the import-time behaviour regardless of test order.
    import subprocess
    import sys

    code = (
        "import sys, cfs; "
        "mods = [m for m in sys.modules if m.startswith('cfs.connectors')]; "
        "assert not mods, mods"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ── One-shot fetch / fetch_sync ─────────────────────────────────────


async def test_fetch_full_product_id(fake_providers):
    ds, result = await cfs.fetch("fake_one:demo", BOX, TR)
    assert isinstance(ds, xr.Dataset)
    assert CanonicalVar.AIR_TEMPERATURE.value in ds
    assert result.product_id == "fake_one:demo"
    assert result.provider == "fake_one"


async def test_fetch_bare_slug_resolves_single_product(fake_providers):
    ds, result = await cfs.fetch("fake_one", BOX, TR)
    assert result.product_id == "fake_one:demo"


async def test_fetch_bare_slug_ambiguous_raises(fake_providers):
    with pytest.raises(ValueError, match="full product ID"):
        await cfs.fetch("fake_two", BOX, TR)
    # ...and the error names the candidates.
    with pytest.raises(ValueError, match="fake_two:alpha"):
        await cfs.fetch("fake_two", BOX, TR)


async def test_fetch_unknown_provider_lists_known(fake_providers):
    with pytest.raises(KeyError, match="Known providers"):
        await cfs.fetch("no_such_provider:thing", BOX, TR)


async def test_fetch_coerces_inputs_and_passes_config(fake_providers):
    ds, result = await cfs.fetch(
        "fake_one:demo",
        bbox=(-1.0, 49.0, 1.0, 51.0),
        time_range=("2020-01-01T00:00", "2020-01-02T00:00"),
        variables=["air_temperature"],
        config={"member": "x1"},
    )
    assert result.bbox == BOX
    assert result.time_range == TR
    assert "'member': 'x1'" in result.provenance
    assert "air_temperature" in result.provenance  # coerced to CanonicalVar


def test_fetch_sync(fake_providers):
    ds, result = cfs.fetch_sync("fake_one", BOX, TR)
    assert result.product_id == "fake_one:demo"
    assert isinstance(ds, xr.Dataset)


async def test_fetch_sync_inside_running_loop_raises(fake_providers):
    with pytest.raises(RuntimeError, match="running event loop"):
        cfs.fetch_sync("fake_one", BOX, TR)


# ── configure() ─────────────────────────────────────────────────────


@pytest.fixture
def clean_settings():
    saved = {k: v for k, v in os.environ.items() if k.startswith("CFS_")}
    for k in saved:
        del os.environ[k]
    get_settings.cache_clear()
    yield
    for k in [k for k in os.environ if k.startswith("CFS_")]:
        del os.environ[k]
    os.environ.update(saved)
    get_settings.cache_clear()


def test_configure_overrides_take_effect(clean_settings):
    s = cfs.configure(cache_dir="/tmp/cfs-test-cache", fetch_concurrency=2)
    assert s.cache_dir == "/tmp/cfs-test-cache"
    # The cached accessor (what connectors call at fetch time) sees it too.
    assert get_settings().cache_dir == "/tmp/cfs-test-cache"
    assert get_settings().fetch_concurrency == 2


def test_configure_bool_and_list_values(clean_settings):
    s = cfs.configure(qc_enabled=False, api_keys=["k1", "k2"])
    assert s.qc_enabled is False
    assert s.api_keys == ["k1", "k2"]


def test_configure_none_resets_to_default(clean_settings):
    assert cfs.configure(provider_timeout_s=5).provider_timeout_s == 5.0
    assert cfs.configure(provider_timeout_s=None).provider_timeout_s == 120.0


def test_configure_unknown_key_raises(clean_settings):
    with pytest.raises(TypeError, match="Unknown CFS setting"):
        cfs.configure(no_such_setting=1)
    # And a bad call must not have clobbered the cache with partial state.
    assert get_settings().provider_timeout_s == 120.0
