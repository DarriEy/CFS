# SPDX-License-Identifier: MIT
"""Tests for the SYMFLUENCE integration (``cfs.integrations.symfluence``).

Three tiers:

1. **No-SYMFLUENCE tests** (always run): the integration module imports
   cleanly when SYMFLUENCE is absent (simulated by blocking ``symfluence`` in
   ``sys.modules``) and ``register()`` raises ``ImportError`` naturally.
2. **Vocabulary tests** (always run): the CFS->CFIF mapping is consistent
   with CFS's canonical vocabulary.
3. **Integration tests** (``pytest.importorskip("symfluence")``): registry
   registration, entry-point discovery, and an end-to-end ``download()``
   against a monkeypatched ``cfs.fetch_sync`` returning a tiny synthetic
   canonical-v1 dataset.
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
    from symfluence.core.registries import R

    from cfs.integrations.symfluence import CFSDatasetHandler, CFSForcingAcquirer, register

    register()
    register()  # re-registration must not raise (Registry.add overwrites)

    assert R.acquisition_handlers.get("CFS") is CFSForcingAcquirer
    assert R.dataset_handlers.get("cfs") is CFSDatasetHandler

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
    from symfluence.core.registries import R

    assert "CFS" in R.acquisition_handlers
    assert "cfs" in R.dataset_handlers


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
