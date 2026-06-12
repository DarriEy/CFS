# SPDX-License-Identifier: MIT
"""Tests for the SYMFLUENCE integration (``cfs.integrations.symfluence``).

Four tiers:

1. **No-SYMFLUENCE tests** (always run): the integration module imports
   cleanly when SYMFLUENCE is absent (simulated by blocking ``symfluence`` in
   ``sys.modules``) and ``register()`` raises ``ImportError`` naturally.
2. **Vocabulary tests** (always run): the CFS->CFIF mapping is consistent
   with CFS's canonical vocabulary.
3. **Integration tests** (``pytest.importorskip("symfluence")``): registry
   registration, entry-point discovery, and an end-to-end ``download()``
   against a monkeypatched ``cfs.fetch_sync`` returning a tiny synthetic
   canonical-v1 dataset.
4. **Shadow (drop-in) tests** (``pytest.importorskip("symfluence")``): the
   parity-gated shadow wrappers replace the native registry entries under the
   same names, route DATA_ACCESS/`<NAME>_BACKEND`, delegate native mode to the
   captured native class, and the dataset-handler shadows self-detect
   canonical-v1 vs native-format raw files.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import sys
from datetime import datetime

import pytest

# ── Tier 1: defensive import without SYMFLUENCE ─────────────────────


def test_module_imports_without_symfluence():
    """``import cfs.integrations.symfluence`` must succeed without SYMFLUENCE."""
    saved = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name == "symfluence"
        or name.startswith("symfluence.")
        or name == "cfs.integrations.symfluence"
    }
    for name in saved:
        del sys.modules[name]
    # None in sys.modules makes any `import symfluence...` raise ImportError.
    sys.modules["symfluence"] = None  # type: ignore[assignment]
    try:
        mod = importlib.import_module("cfs.integrations.symfluence")
        assert mod.HAVE_SYMFLUENCE is False
        # Classes still exist (bases degraded to object) so introspection works.
        assert isinstance(mod.CFSForcingAcquirer, type)
        assert isinstance(mod.CFSDatasetHandler, type)
        # register() raises ImportError naturally — SYMFLUENCE's plugin
        # discovery logs and skips a failing entry point.
        with pytest.raises(ImportError):
            mod.register()
    finally:
        sys.modules.pop("symfluence", None)
        sys.modules.pop("cfs.integrations.symfluence", None)
        sys.modules.update(saved)


# ── Tier 2: variable-mapping consistency with the CFS vocabulary ────


def test_mapping_sources_are_cfs_canonical_names():
    from cfs.core.vocabulary import CANONICAL, CanonicalVar
    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING

    canonical_names = {v.value for v in CanonicalVar}
    assert set(CFS_TO_CFIF_MAPPING) <= canonical_names
    # Every mapped source name has a canonical spec.
    for source in CFS_TO_CFIF_MAPPING:
        assert CanonicalVar(source) in CANONICAL


def test_mapping_covers_vocabulary_except_dewpoint():
    """Append-only guard: a new canonical variable must get a mapping decision."""
    from cfs.core.vocabulary import CanonicalVar
    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING

    unmapped = {v.value for v in CanonicalVar} - set(CFS_TO_CFIF_MAPPING)
    # dewpoint_temperature has no CFIF counterpart and passes through unchanged.
    assert unmapped == {"dewpoint_temperature"}


def test_mapping_is_identity_today():
    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING

    assert all(src == dst for src, dst in CFS_TO_CFIF_MAPPING.items())


# ── Tier 3: full integration (requires SYMFLUENCE installed) ────────


def _make_symfluence_config(tmp_path, **extra):
    """Minimal flat config dict accepted by SYMFLUENCE's config coercion.

    Mirrors SYMFLUENCE's own acquisition-test fixture
    (tests/fixtures/acquisition_fixtures.py: MockConfigFactory.create).
    """
    config = {
        "DOMAIN_NAME": "test_domain",
        "DATA_DIR": str(tmp_path),
        # north/west/south/east
        "BOUNDING_BOX_COORDS": "47.0/8.0/46.0/9.0",
        "EXPERIMENT_TIME_START": "2020-01-01",
        "EXPERIMENT_TIME_END": "2020-01-02",
        "FORCING_DATASET": "CFS",
        "FORCE_DOWNLOAD": False,
        "PROJECT_DIR": str(tmp_path / "domain_test_domain"),
        "SYMFLUENCE_DATA_DIR": str(tmp_path),
        "SYMFLUENCE_CODE_DIR": str(tmp_path / "code"),
        "EXPERIMENT_ID": "test_run",
        "DOMAIN_DEFINITION_METHOD": "lumped",
        "SUB_GRID_DISCRETIZATION": "lumped",
        "HYDROLOGICAL_MODEL": "SUMMA",
    }
    config.update(extra)
    return config


def _canonical_dataset(xr, np):
    """Tiny synthetic canonical-v1 dataset (regular lat/lon grid)."""
    time = [datetime(2020, 1, 1, h) for h in range(4)]
    lat = np.array([46.25, 46.75])
    lon = np.array([8.25, 8.75])
    shape = (len(time), len(lat), len(lon))
    return xr.Dataset(
        {
            "air_temperature": (
                ("time", "latitude", "longitude"),
                np.full(shape, 275.0),
                {"standard_name": "air_temperature", "units": "K",
                 "long_name": "Near-surface (2 m) air temperature",
                 "cfs_source_name": "t2m"},
            ),
            "precipitation_flux": (
                ("time", "latitude", "longitude"),
                np.full(shape, 1e-5),
                {"standard_name": "precipitation_flux", "units": "kg m-2 s-1",
                 "long_name": "Precipitation rate",
                 "cfs_source_name": "tp"},
            ),
        },
        coords={"time": time, "latitude": lat, "longitude": lon},
        attrs={"cfs_schema": "canonical-v1"},
    )


def _fetch_result(variables):
    from cfs.core.models import BoundingBox, FetchResult, TimeRange
    from cfs.core.vocabulary import CanonicalVar

    return FetchResult(
        product_id="fake:demo",
        provider="fake",
        variables=[CanonicalVar(v) for v in variables],
        bbox=BoundingBox(min_lon=8.0, min_lat=46.0, max_lon=9.0, max_lat=47.0),
        time_range=TimeRange(start=datetime(2020, 1, 1), end=datetime(2020, 1, 2)),
        n_times=4,
        n_lat=2,
        n_lon=2,
        resolution_deg=0.5,
        warnings=["synthetic QC note"],
    )


def test_register_is_discoverable_and_idempotent():
    pytest.importorskip("symfluence")
    reg = _registries()

    from cfs.integrations.symfluence import CFSDatasetHandler, CFSForcingAcquirer, register

    register()
    register()  # re-registration must not raise (Registry.add overwrites)

    assert reg.acquisition_handlers.get("CFS") is CFSForcingAcquirer
    assert reg.dataset_handlers.get("cfs") is CFSDatasetHandler

    # The lookup facade sees it too (case-insensitive keys).
    from symfluence.data.acquisition.registry import AcquisitionRegistry

    assert "CFS" in [name.upper() for name in AcquisitionRegistry.list_datasets()]


def test_entry_point_registered_in_metadata():
    pytest.importorskip("symfluence")
    eps = importlib.metadata.entry_points(group="symfluence.plugins")
    by_name = {ep.name: ep.value for ep in eps}
    assert by_name.get("cfs") == "cfs.integrations.symfluence:register"


def test_import_symfluence_autoregisters_cfs():
    """Entry-point auto-discovery: `import symfluence` alone registers 'CFS'."""
    pytest.importorskip("symfluence")
    import symfluence  # noqa: F401  (bootstrap runs _discover_plugins)

    reg = _registries()
    assert "CFS" in reg.acquisition_handlers
    assert "cfs" in reg.dataset_handlers


def test_mapping_targets_exist_in_cfif():
    pytest.importorskip("symfluence")
    from symfluence.data.preprocessing.cfif.variables import CFIF_VARIABLES

    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING

    assert set(CFS_TO_CFIF_MAPPING.values()) <= set(CFIF_VARIABLES)


def test_download_writes_netcdf_from_synthetic_fetch(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    import cfs
    from cfs.integrations.symfluence import CFSForcingAcquirer

    calls = {}

    def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
        calls["product"] = product
        calls["bbox"] = bbox
        calls["time_range"] = time_range
        calls["variables"] = variables
        calls["config"] = config
        ds = _canonical_dataset(xr, np)
        return ds, _fetch_result(["air_temperature", "precipitation_flux"])

    monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)
    # Keep the test hermetic: no real connector module imports.
    monkeypatch.setattr(cfs, "discover", lambda: None)
    monkeypatch.setattr(cfs, "list_providers", lambda: ["fake"])

    config = _make_symfluence_config(
        tmp_path,
        CFS_PRODUCT="fake:demo",
        CFS_VARIABLES="air_temperature, precipitation_flux",
        CFS_CONNECTOR_CONFIG={"members": ["demo"]},
    )
    handler = CFSForcingAcquirer(config, logging.getLogger("test_cfs"))
    out_dir = tmp_path / "raw"
    out_path = handler.download(out_dir)

    # The call wired bbox (min_lon, min_lat, max_lon, max_lat) and options through.
    assert calls["product"] == "fake:demo"
    assert calls["bbox"] == (8.0, 46.0, 9.0, 47.0)
    assert calls["variables"] == ["air_temperature", "precipitation_flux"]
    assert calls["config"] == {"members": ["demo"]}

    # A canonical NetCDF landed in output_dir with the handler naming scheme.
    assert out_path.exists()
    assert out_path.parent == out_dir
    assert out_path.name == "domain_test_domain_cfs_fake_demo_20200101_20200102.nc"
    with xr.open_dataset(out_path) as written:
        assert written.attrs["cfs_schema"] == "canonical-v1"
        assert "air_temperature" in written.data_vars
        assert set(written.dims) == {"time", "latitude", "longitude"}

    # Idempotency: a second call skips the fetch entirely.
    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("fetch_sync called despite existing output")

    monkeypatch.setattr(cfs, "fetch_sync", boom)
    assert handler.download(out_dir) == out_path


def test_download_requires_cfs_product(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    import cfs
    from cfs.integrations.symfluence import CFSForcingAcquirer

    monkeypatch.setattr(cfs, "discover", lambda: None)
    monkeypatch.setattr(cfs, "list_providers", lambda: ["aorc", "era5_arco"])

    config = _make_symfluence_config(tmp_path)  # no CFS_PRODUCT
    handler = CFSForcingAcquirer(config, logging.getLogger("test_cfs"))
    with pytest.raises(ValueError, match="CFS_PRODUCT is required.*aorc, era5_arco"):
        handler.download(tmp_path / "raw")


def test_download_rejects_unknown_provider(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    import cfs
    from cfs.integrations.symfluence import CFSForcingAcquirer

    monkeypatch.setattr(cfs, "discover", lambda: None)
    monkeypatch.setattr(cfs, "list_providers", lambda: ["aorc", "era5_arco"])

    config = _make_symfluence_config(tmp_path, CFS_PRODUCT="nope:product")
    handler = CFSForcingAcquirer(config, logging.getLogger("test_cfs"))
    with pytest.raises(ValueError, match="Unknown CFS product 'nope:product'"):
        handler.download(tmp_path / "raw")


def test_dataset_handler_processes_regular_grid(tmp_path):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    from cfs.integrations.symfluence import CFSDatasetHandler

    handler = CFSDatasetHandler(
        _make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path
    )
    assert handler.get_coordinate_names() == ("latitude", "longitude")
    assert handler.needs_merging() is True

    ds = _canonical_dataset(xr, np)
    out = handler.process_dataset(ds)
    # Identity rename: canonical names are already CFIF names.
    assert set(out.data_vars) == {"air_temperature", "precipitation_flux"}
    # CFIF attributes were applied.
    assert out["air_temperature"].attrs["units"] == "K"
    assert out["precipitation_flux"].attrs["units"] == "kg m-2 s-1"
    assert out["precipitation_flux"].attrs["standard_name"] == "precipitation_flux"


def test_dataset_handler_rejects_projected_grid(tmp_path):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    from cfs.integrations.symfluence import CFSDatasetHandler

    # Projected canonical layout: native y/x dims, 2-D lat/lon coordinates.
    y = np.arange(3)
    x = np.arange(4)
    lat2d = np.linspace(46.0, 47.0, 12).reshape(3, 4)
    lon2d = np.linspace(8.0, 9.0, 12).reshape(3, 4)
    ds = xr.Dataset(
        {
            "air_temperature": (
                ("time", "y", "x"),
                np.full((2, 3, 4), 275.0),
            )
        },
        coords={
            "time": [datetime(2020, 1, 1), datetime(2020, 1, 1, 1)],
            "y": y,
            "x": x,
            "latitude": (("y", "x"), lat2d),
            "longitude": (("y", "x"), lon2d),
        },
        attrs={"cfs_schema": "canonical-v1"},
    )

    handler = CFSDatasetHandler(
        _make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path
    )
    with pytest.raises(NotImplementedError, match="regular latitude/longitude grids only"):
        handler.process_dataset(ds)


# ── Tier 4: drop-in shadow wrappers (requires SYMFLUENCE) ────────────


def _registries():
    import symfluence  # noqa: F401  (bootstrap + plugin discovery)
    from symfluence.core.registries import R

    return R


def _rich_canonical_dataset(xr, np):
    """Canonical-v1 dataset carrying the ERA5 derivation inputs (u/v/dewpoint/pressure)."""
    ds = _canonical_dataset(xr, np)
    shape = tuple(ds.sizes[d] for d in ("time", "latitude", "longitude"))
    dims = ("time", "latitude", "longitude")
    ds["eastward_wind"] = xr.DataArray(
        np.full(shape, 3.0, dtype="float32"), dims=dims,
        attrs={"standard_name": "eastward_wind", "units": "m s-1"})
    ds["northward_wind"] = xr.DataArray(
        np.full(shape, 4.0, dtype="float32"), dims=dims,
        attrs={"standard_name": "northward_wind", "units": "m s-1"})
    ds["dewpoint_temperature"] = xr.DataArray(
        np.full(shape, 270.0, dtype="float32"), dims=dims,
        attrs={"standard_name": "dew_point_temperature", "units": "K"})
    ds["surface_air_pressure"] = xr.DataArray(
        np.full(shape, 90000.0, dtype="float32"), dims=dims,
        attrs={"standard_name": "surface_air_pressure", "units": "Pa"})
    return ds


def test_shadow_replaces_native_registry_entry_and_captures_native():
    pytest.importorskip("symfluence")
    reg = _registries()
    from symfluence.data.acquisition.base import BaseAcquisitionHandler
    from symfluence.data.acquisition.handlers.era5 import ERA5Acquirer

    wrapper = reg.acquisition_handlers.get("ERA5")
    assert getattr(wrapper, "_cfs_shadow", False), "registry entry for 'ERA5' is not the shadow"
    assert wrapper is not ERA5Acquirer
    # The captured native class is the genuine in-tree acquirer, not a wrapper.
    assert wrapper._native_cls is ERA5Acquirer
    assert issubclass(wrapper, BaseAcquisitionHandler)


def test_shadow_register_is_idempotent():
    """Double registration must never capture a shadow as 'the native class'."""
    pytest.importorskip("symfluence")
    reg = _registries()
    from symfluence.data.acquisition.handlers.era5 import ERA5Acquirer

    from cfs.integrations.symfluence import register

    register()
    register()
    wrapper = reg.acquisition_handlers.get("ERA5")
    assert wrapper._native_cls is ERA5Acquirer
    assert not getattr(wrapper._native_cls, "_cfs_shadow", False)


def test_all_shadow_aliases_are_installed():
    pytest.importorskip("symfluence")
    reg = _registries()

    from cfs.integrations.symfluence import SHADOW_SPECS

    for spec in SHADOW_SPECS:
        for alias in spec.aliases:
            cls = reg.acquisition_handlers.get(alias)
            assert getattr(cls, "_cfs_shadow", False), f"'{alias}' is not shadowed"
        for key in spec.dataset_keys:
            cls = reg.dataset_handlers.get(key)
            assert getattr(cls, "_cfs_shadow", False), f"dataset key '{key}' is not shadowed"


def test_unvalidated_datasets_are_not_shadowed():
    """Parity gate: MSWEP / EM-EARTH stay native until live validation."""
    pytest.importorskip("symfluence")
    reg = _registries()
    from symfluence.data.acquisition.handlers.em_earth import EMEarthAcquirer
    from symfluence.data.acquisition.handlers.mswep import MSWEPAcquirer

    assert reg.acquisition_handlers.get("MSWEP") is MSWEPAcquirer
    assert reg.acquisition_handlers.get("EM-EARTH") is EMEarthAcquirer
    assert not getattr(reg.acquisition_handlers.get("MSWEP"), "_cfs_shadow", False)


def test_shadow_native_mode_delegates_to_captured_class(tmp_path, monkeypatch):
    """DATA_ACCESS unset -> exactly the native class runs; config untouched."""
    pytest.importorskip("symfluence")
    import cfs

    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("ERA5")
    native_cls = wrapper_cls._native_cls

    calls = {}

    def fake_native_download(self, output_dir):
        calls["cls"] = type(self)
        calls["output_dir"] = output_dir
        return output_dir / "native_era5.nc"

    monkeypatch.setattr(native_cls, "download", fake_native_download)
    monkeypatch.setattr(
        cfs, "fetch_sync",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("community fetch in native mode")),
    )

    config = _make_symfluence_config(tmp_path, FORCING_DATASET="ERA5")  # no DATA_ACCESS
    before = dict(config)
    handler = wrapper_cls(config, logging.getLogger("test_cfs"))
    out = handler.download(tmp_path / "raw")

    assert calls["cls"] is native_cls
    assert calls["output_dir"] == tmp_path / "raw"
    assert out == tmp_path / "raw" / "native_era5.nc"
    assert config == before  # the wrapper never mutates the user config


def test_shadow_cloud_mode_also_delegates_native(tmp_path, monkeypatch):
    """DATA_ACCESS: cloud is not 'community' -> native delegation."""
    pytest.importorskip("symfluence")
    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("AORC")
    native_cls = wrapper_cls._native_cls

    called = {}
    monkeypatch.setattr(
        native_cls, "download", lambda self, output_dir: called.setdefault("dir", output_dir)
    )
    config = _make_symfluence_config(tmp_path, FORCING_DATASET="AORC", DATA_ACCESS="cloud")
    wrapper_cls(config, logging.getLogger("test_cfs")).download(tmp_path / "raw")
    assert called["dir"] == tmp_path / "raw"


def test_shadow_community_mode_fetches_and_writes_canonical(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    import cfs

    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("ERA5")

    calls = {}

    def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
        calls["product"] = product
        calls["bbox"] = bbox
        calls["variables"] = variables
        return _rich_canonical_dataset(xr, np), _fetch_result(["air_temperature"])

    monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)

    config = _make_symfluence_config(tmp_path, FORCING_DATASET="ERA5", DATA_ACCESS="community")
    handler = wrapper_cls(config, logging.getLogger("test_cfs"))
    out_path = handler.download(tmp_path / "raw")

    assert calls["product"] == "era5_arco:single_levels"
    assert calls["bbox"] == (8.0, 46.0, 9.0, 47.0)
    assert calls["variables"] is None  # ERA5 community fetch takes all the product offers

    assert out_path.exists() and out_path.parent == tmp_path / "raw"
    with xr.open_dataset(out_path) as written:
        # canonical-v1 attrs intact: the dataset-handler shadows detect this key.
        assert written.attrs["cfs_schema"] == "canonical-v1"
        # SYMFLUENCE-native derivations were added with the native op order.
        assert float(written["wind_speed"].isel(time=0, latitude=0, longitude=0)) == pytest.approx(5.0)
        assert "specific_humidity" in written.data_vars

    # Skip-if-exists semantics preserved.
    monkeypatch.setattr(
        cfs, "fetch_sync",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch despite existing output")),
    )
    assert handler.download(tmp_path / "raw") == out_path


def test_shadow_per_dataset_backend_optout_wins(tmp_path, monkeypatch):
    """DATA_ACCESS: community + ERA5_BACKEND: native -> the native class runs."""
    pytest.importorskip("symfluence")
    import cfs

    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("ERA5")
    native_cls = wrapper_cls._native_cls

    called = {}
    monkeypatch.setattr(
        native_cls, "download", lambda self, output_dir: called.setdefault("dir", output_dir)
    )
    monkeypatch.setattr(
        cfs, "fetch_sync",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("community fetch despite opt-out")),
    )

    config = _make_symfluence_config(
        tmp_path, FORCING_DATASET="ERA5", DATA_ACCESS="community", ERA5_BACKEND="native"
    )
    wrapper_cls(config, logging.getLogger("test_cfs")).download(tmp_path / "raw")
    assert called["dir"] == tmp_path / "raw"


def test_nldas_community_variables_translate_native_names(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    import cfs

    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("NLDAS")

    calls = {}

    def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
        calls["product"] = product
        calls["variables"] = variables
        return _canonical_dataset(xr, np), _fetch_result(["air_temperature"])

    monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)

    config = _make_symfluence_config(
        tmp_path,
        FORCING_DATASET="NLDAS",
        DATA_ACCESS="community",
        # Native NLDAS-2 v2.0 names + one with no canonical counterpart.
        NLDAS_VARIABLES=["Tair", "Rainf", "CAPE"],
    )
    wrapper_cls(config, logging.getLogger("test_cfs")).download(tmp_path / "raw")

    assert calls["product"] == "nldas:fora0125_h"
    assert calls["variables"] == ["air_temperature", "precipitation_flux"]  # CAPE dropped, loudly


def test_nex_gddp_community_builds_product_from_native_keys(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    import cfs

    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("NEX-GDDP")

    calls = []

    def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
        calls.append({"product": product, "time_range": time_range, "config": config})
        return _canonical_dataset(xr, np), _fetch_result(["air_temperature"])

    monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)

    config = _make_symfluence_config(
        tmp_path,
        FORCING_DATASET="NEX-GDDP",
        DATA_ACCESS="community",
        NEX_MODELS=["ACCESS-CM2"],
        NEX_SCENARIOS=["historical"],
        NEX_ENSEMBLES=["r1i1p1f1"],
        EXPERIMENT_TIME_START="2010-01-01",
        EXPERIMENT_TIME_END="2020-01-01",
    )
    handler = wrapper_cls(config, logging.getLogger("test_cfs"))
    out_dir = handler.download(tmp_path / "raw")

    assert out_dir == tmp_path / "raw"
    assert len(calls) == 1
    assert calls[0]["product"] == "nex_gddp:historical"
    assert calls[0]["config"] == {"model": "ACCESS-CM2", "member": "r1i1p1f1"}
    # The window is clipped to the historical scenario extent (<= 2014).
    assert calls[0]["time_range"][1].year == 2014

    files = list((tmp_path / "raw").glob("*.nc"))
    assert len(files) == 1
    with xr.open_dataset(files[0]) as written:
        assert written.attrs["cfs_schema"] == "canonical-v1"
        # Synthetic pressure mirrors the native handler (sea-level constant).
        assert float(written["surface_air_pressure"].max()) == pytest.approx(101325.0)


def test_nex_gddp_community_requires_models(tmp_path):
    pytest.importorskip("symfluence")
    reg = _registries()
    wrapper_cls = reg.acquisition_handlers.get("NEX-GDDP")

    config = _make_symfluence_config(tmp_path, FORCING_DATASET="NEX-GDDP", DATA_ACCESS="community")
    handler = wrapper_cls(config, logging.getLogger("test_cfs"))
    with pytest.raises(ValueError, match="NEX_MODELS must be set"):
        handler.download(tmp_path / "raw")


# ── Tier 4b: self-detecting dataset-handler shadows ──────────────────


def test_dataset_shadow_processes_canonical_dataset(tmp_path):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    reg = _registries()
    from symfluence.data.preprocessing.dataset_handlers.era5_utils import ERA5Handler

    shadow_cls = reg.dataset_handlers.get("era5")
    assert getattr(shadow_cls, "_cfs_shadow", False)
    assert shadow_cls._native_cls is ERA5Handler

    handler = shadow_cls(_make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path)
    out = handler.process_dataset(_canonical_dataset(xr, np))
    # Canonical path: CFIF attributes applied, canonical names kept (identity).
    assert out["precipitation_flux"].attrs["units"] == "kg m-2 s-1"
    assert out["air_temperature"].attrs["units"] == "K"


def test_dataset_shadow_delegates_native_format_dataset(tmp_path, monkeypatch):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    reg = _registries()
    shadow_cls = reg.dataset_handlers.get("era5")
    native_cls = shadow_cls._native_cls

    seen = {}

    def fake_process(self, ds):
        seen["cls"] = type(self)
        return ds

    monkeypatch.setattr(native_cls, "process_dataset", fake_process)

    handler = shadow_cls(_make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path)
    native_ds = _canonical_dataset(xr, np)
    native_ds.attrs.pop("cfs_schema")  # native-format file: no canonical marker
    handler.process_dataset(native_ds)
    assert seen["cls"] is native_cls


def test_dataset_shadow_dir_probe_caches_canonical_verdict(tmp_path):
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    reg = _registries()
    shadow_cls = reg.dataset_handlers.get("era5")

    project_dir = tmp_path / "domain_test_domain"
    raw_dir = project_dir / "data" / "forcing" / "raw_data"
    raw_dir.mkdir(parents=True)
    _canonical_dataset(xr, np).to_netcdf(raw_dir / "domain_test_domain_cfs_demo.nc")

    handler = shadow_cls(
        _make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), project_dir
    )
    assert handler.get_coordinate_names() == ("latitude", "longitude")
    assert handler.get_variable_mapping() == {k: k for k in handler.get_variable_mapping()}
    assert handler.needs_merging() is True
    assert handler._canonical_verdict is True  # cached after one probe


def test_dataset_shadow_defaults_to_native_without_files(tmp_path):
    """No raw files yet -> exact native behavior (mapping comes from the native handler)."""
    pytest.importorskip("symfluence")
    reg = _registries()
    from symfluence.data.preprocessing.dataset_handlers.era5_utils import ERA5Handler

    shadow_cls = reg.dataset_handlers.get("era5")
    handler = shadow_cls(_make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path)
    native = ERA5Handler(_make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path)
    assert handler.get_variable_mapping() == native.get_variable_mapping()
    assert handler.get_coordinate_names() == native.get_coordinate_names()


def test_nldas_dataset_key_is_canonical_only(tmp_path):
    """SYMFLUENCE has no native NLDAS dataset handler; the CFS key serves canonical files only."""
    pytest.importorskip("symfluence")
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    reg = _registries()
    shadow_cls = reg.dataset_handlers.get("nldas")
    assert getattr(shadow_cls, "_cfs_shadow", False)
    assert shadow_cls._native_cls is None

    handler = shadow_cls(_make_symfluence_config(tmp_path), logging.getLogger("test_cfs"), tmp_path)
    # Canonical files work (this is the community-acquired path).
    out = handler.process_dataset(_canonical_dataset(xr, np))
    assert out["air_temperature"].attrs["units"] == "K"
    # Native-format files raise a clear error instead of mis-processing.
    native_ds = _canonical_dataset(xr, np)
    native_ds.attrs.pop("cfs_schema")
    with pytest.raises(NotImplementedError, match="canonical-v1 files only"):
        handler.process_dataset(native_ds)
