# SPDX-License-Identifier: MIT
"""Tests for the SYMFLUENCE integration (``cfs.integrations.symfluence``).

Tiers:

1. **No-SYMFLUENCE tests** (always run): the integration module imports
   cleanly when SYMFLUENCE is absent and ``register()`` raises ``ImportError``
   naturally.
2. **Vocabulary tests** (always run): the CFS->CFIF mapping is consistent
   with CFS's canonical vocabulary.
3. **Backend tests** (``pytest.importorskip("symfluence")``): registration via
   the entry point, capability shape (parity grades, grid classes, the
   ungated parallel-name 'CFS' entry), ``acquire()`` against a monkeypatched
   ``cfs.fetch_sync`` writing canonical NetCDF + the acquisition manifest,
   decline/error paths mapped onto the protocol taxonomy, and the
   framework-side ungated policy.
4. **canonical-v1 dataset handler** (requires SYMFLUENCE): the ONE
   schema-keyed handler — CFIF contract methods on regular grids, and the
   projected-grid (RDRS-class) support: wind_speed derivation, monthly
   consolidated-store splits, per-cell polygon shapefiles on a synthetic
   rotated grid and on the real exp10 artifact when present.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytest

EXP10_ARTIFACT = Path("/tmp/parity-exp/community-rdrs/rdrs_cfs_canonical.nc")


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
        # Classes still exist (bases degraded) so introspection works.
        assert isinstance(mod.CommunityForcingBackend, type)
        assert isinstance(mod.CanonicalV1Handler, type)
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
    for source in CFS_TO_CFIF_MAPPING:
        assert CanonicalVar(source) in CANONICAL


def test_mapping_covers_vocabulary_except_dewpoint():
    """Append-only guard: a new canonical variable must get a mapping decision."""
    from cfs.core.vocabulary import CanonicalVar
    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING

    unmapped = {v.value for v in CanonicalVar} - set(CFS_TO_CFIF_MAPPING)
    assert unmapped == {"dewpoint_temperature"}


def test_mapping_is_identity_today():
    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING

    assert all(src == dst for src, dst in CFS_TO_CFIF_MAPPING.items())


def test_spec_variables_are_cfif_subsets():
    """Capability declarations must stay inside the shared vocabulary."""
    from cfs.integrations.symfluence import CFS_TO_CFIF_MAPPING, DATASET_SPECS

    cfif_names = set(CFS_TO_CFIF_MAPPING.values())
    for spec in DATASET_SPECS:
        assert spec.variables <= cfif_names, spec.family


# ── Shared synthetic data ─────────────────────────────────────────────


def _make_symfluence_config(tmp_path, **extra):
    """Minimal flat config dict accepted by SYMFLUENCE's config coercion."""
    config = {
        "DOMAIN_NAME": "test_domain",
        "DATA_DIR": str(tmp_path),
        # north/west/south/east
        "BOUNDING_BOX_COORDS": "47.0/8.0/46.0/9.0",
        "EXPERIMENT_TIME_START": "2020-01-01",
        "EXPERIMENT_TIME_END": "2020-01-02",
        "FORCING_DATASET": "ERA5",
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
                np.full(shape, 275.0, dtype="float32"),
                {"standard_name": "air_temperature", "units": "K"},
            ),
            "precipitation_flux": (
                ("time", "latitude", "longitude"),
                np.full(shape, 1e-5, dtype="float32"),
                {"standard_name": "precipitation_flux", "units": "kg m-2 s-1"},
            ),
        },
        coords={"time": time, "latitude": lat, "longitude": lon},
        attrs={"cfs_schema": "canonical-v1"},
    )


def _projected_canonical_dataset(xr, np, n_times=72):
    """Synthetic canonical-v1 PROJECTED dataset (rotated-pole layout).

    Mirrors the canonical RDRS/CaSR layout: native ``rlat``/``rlon`` dims,
    2-D ``latitude``/``longitude`` auxiliary coordinates, hourly time.
    """
    import pandas as pd

    times = pd.date_range("2018-06-01", periods=n_times, freq="h")
    rlat = np.linspace(-14.58, -13.86, 4).astype("float32")
    rlon = np.linspace(-15.06, -14.34, 5).astype("float32")
    lat2d = (41.5 + np.add.outer(np.arange(4) * 0.1, np.arange(5) * 0.02)).astype("float32")
    lon2d = (-112.3 + np.add.outer(np.arange(4) * 0.03, np.arange(5) * 0.15)).astype("float32")
    shape = (len(times), len(rlat), len(rlon))

    def var(value, units, name):
        return (
            ("time", "rlat", "rlon"),
            np.full(shape, value, dtype="float32"),
            {"standard_name": name, "units": units},
        )

    return xr.Dataset(
        {
            "air_temperature": var(280.0, "K", "air_temperature"),
            "precipitation_flux": var(2e-5, "kg m-2 s-1", "precipitation_flux"),
            "specific_humidity": var(0.005, "kg kg-1", "specific_humidity"),
            "surface_air_pressure": var(90000.0, "Pa", "surface_air_pressure"),
            "eastward_wind": var(3.0, "m s-1", "eastward_wind"),
            "northward_wind": var(4.0, "m s-1", "northward_wind"),
            "surface_downwelling_shortwave_flux": var(200.0, "W m-2", "rsds"),
            "surface_downwelling_longwave_flux": var(300.0, "W m-2", "rlds"),
        },
        coords={
            "time": times,
            "rlat": rlat,
            "rlon": rlon,
            "latitude": (("rlat", "rlon"), lat2d),
            "longitude": (("rlat", "rlon"), lon2d),
        },
        attrs={"cfs_schema": "canonical-v1"},
    )


def _lcc_canonical_dataset(xr, np, n_times=72, freq="h", n_y=4, n_x=5):
    """Synthetic canonical-v1 PROJECTED dataset (Lambert-conformal layout).

    Mirrors the canonical CONUS404 / HRRR / NWM3 layout: native ``y``/``x``
    dims carrying 1-D projected-metre coordinates, 2-D ``latitude``/
    ``longitude`` auxiliary coordinates. With ``freq="D"`` (and noon-anchored
    timestamps) it doubles as the Daymet daily layout, which shares the same
    grid family (1-D LCC x/y + 2-D lat/lon).
    """
    import pandas as pd

    anchor = "2018-06-01 12:00" if freq == "D" else "2018-06-01"
    times = pd.date_range(anchor, periods=n_times, freq=freq)
    y = (np.arange(n_y) * 4000.0 + 100000.0).astype("float64")
    x = (np.arange(n_x) * 4000.0 - 500000.0).astype("float64")
    lat2d = (39.5 + np.add.outer(np.arange(n_y) * 0.036, np.arange(n_x) * 0.003)).astype("float32")
    lon2d = (-105.5 + np.add.outer(np.arange(n_y) * 0.004, np.arange(n_x) * 0.045)).astype("float32")
    shape = (len(times), n_y, n_x)

    def var(value, units, name):
        data = np.full(shape, value, dtype="float32")
        return (("time", "y", "x"), data, {"standard_name": name, "units": units})

    return xr.Dataset(
        {
            "air_temperature": var(281.0, "K", "air_temperature"),
            "precipitation_flux": var(3e-5, "kg m-2 s-1", "precipitation_flux"),
            "eastward_wind": var(3.0, "m s-1", "eastward_wind"),
            "northward_wind": var(4.0, "m s-1", "northward_wind"),
            "surface_downwelling_shortwave_flux": var(220.0, "W m-2", "rsds"),
        },
        coords={
            "time": times,
            "y": y,
            "x": x,
            "latitude": (("y", "x"), lat2d),
            "longitude": (("y", "x"), lon2d),
        },
        attrs={"cfs_schema": "canonical-v1"},
    )


def _fetch_result(variables, product_id="fake:demo", provider="fake"):
    from cfs.core.models import BoundingBox, FetchResult, TimeRange

    return FetchResult(
        product_id=product_id,
        provider=provider,
        variables=list(variables),
        bbox=BoundingBox(min_lon=8.0, min_lat=46.0, max_lon=9.0, max_lat=47.0),
        time_range=TimeRange(start=datetime(2020, 1, 1), end=datetime(2020, 1, 2)),
        n_times=4,
        n_lat=2,
        n_lon=2,
        resolution_deg=0.5,
        warnings=["synthetic QC note"],
    )


def _request(tmp_path, dataset="ERA5", **overrides):
    from symfluence.data.backends.contract import AcquisitionRequest

    kwargs = dict(
        dataset_id=dataset,
        bbox=(46.0, 8.0, 47.0, 9.0),
        window=("2020-01-01 00:00", "2020-01-02 00:00"),
        variables=None,
        target_dir=tmp_path / "raw_data",
    )
    kwargs.update(overrides)
    return AcquisitionRequest(**kwargs)


def _backend(tmp_path, **extra):
    from cfs.integrations.symfluence import CommunityForcingBackend

    return CommunityForcingBackend(
        _make_symfluence_config(tmp_path, **extra), logging.getLogger("test_cfs")
    )


# ── Tier 3: the community acquisition backend ────────────────────────


def test_entry_point_registered_in_metadata():
    pytest.importorskip("symfluence")
    eps = importlib.metadata.entry_points(group="symfluence.plugins")
    by_name = {ep.name: ep.value for ep in eps}
    assert by_name.get("cfs") == "cfs.integrations.symfluence:register"


def test_import_symfluence_autoregisters_backend_and_schema_handler():
    """`import symfluence` alone registers the backend + canonical-v1 handler."""
    pytest.importorskip("symfluence")
    import symfluence  # noqa: F401  (bootstrap runs _discover_plugins)
    from symfluence.core.registries import R

    from cfs.integrations.symfluence import CanonicalV1Handler, CommunityForcingBackend, register

    register()
    register()  # idempotent overwrite

    assert R.acquisition_backends.get("community") is CommunityForcingBackend
    assert R.dataset_handlers.get("canonical-v1") is CanonicalV1Handler


def test_no_registry_shadowing_of_native_entries():
    """The protocol port: native registry slots stay native (no overwriting)."""
    pytest.importorskip("symfluence")
    import symfluence  # noqa: F401
    from symfluence.core.registries import R

    for key in ("ERA5", "AORC", "RDRS"):
        cls = R.acquisition_handlers.get(key)
        assert cls is not None, key
        assert cls.__module__.startswith("symfluence."), (
            f"registry slot '{key}' is no longer the in-tree native handler: {cls}"
        )
        assert not getattr(cls, "_cfs_shadow", False)


class TestCapabilities:
    def test_shape_and_grades(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.contract import DatasetCapability, GridClass, SchemaId

        caps = {c.dataset_id: c for c in _backend(tmp_path).capabilities()}
        assert all(isinstance(c, DatasetCapability) for c in caps.values())
        assert all(c.schema is SchemaId.CANONICAL_V1 for c in caps.values())

        assert caps["ERA5"].parity_grade == "value-identical:2ulp"
        assert caps["NLDAS"].parity_grade == "value-identical:1ulp"
        assert caps["AORC"].parity_grade == "bit-identical"
        assert caps["NEX-GDDP-CMIP6"].parity_grade == "bit-identical"
        assert caps["RDRS"].parity_grade == "bit-identical"
        assert caps["CFS"].parity_grade is None  # ungated by design

        # 2026-06-12 campaign additions (exp13/exp15 + the CASR identity work).
        assert caps["CONUS404"].parity_grade == "value-identical:1ulp"
        assert caps["NWM3_RETROSPECTIVE"].parity_grade == "bit-identical"
        assert caps["CASR"].parity_grade == "bit-identical"

        # 2026-06-12b campaign additions (exp11/exp12/exp14/exp16).
        assert caps["CARRA"].parity_grade == "value-identical:grib-repack"
        assert caps["CERRA"].parity_grade == "value-identical:grib-repack"
        assert caps["HRRR"].parity_grade == "bit-identical"
        assert caps["DAYMET"].parity_grade == "bit-identical:point-sampled"
        assert caps["CARRA"].grid_class is GridClass.REGULAR_LATLON
        assert caps["CERRA"].grid_class is GridClass.REGULAR_LATLON
        assert caps["HRRR"].grid_class is GridClass.PROJECTED
        assert caps["DAYMET"].grid_class is GridClass.PROJECTED
        assert caps["CARRA"].auth == {"cds"} and caps["CERRA"].auth == {"cds"}
        assert caps["DAYMET"].auth == {"earthdata"}

        assert caps["RDRS"].grid_class is GridClass.PROJECTED
        assert caps["CONUS404"].grid_class is GridClass.PROJECTED
        assert caps["NWM3_RETROSPECTIVE"].grid_class is GridClass.PROJECTED
        assert caps["CASR"].grid_class is GridClass.PROJECTED
        assert caps["ERA5"].grid_class is GridClass.REGULAR_LATLON
        # aliases are requestable directly
        assert {"NLDAS2", "NLDAS-2", "NEX-GDDP", "RDRS_v3.1"} <= set(caps)

    def test_casr_is_an_alias_of_the_rdrs_product(self, tmp_path):
        """CASR maps onto the same CFS product as RDRS (same ECCC CaSR store)."""
        from cfs.integrations.symfluence import _spec_for

        casr, rdrs = _spec_for("CASR"), _spec_for("RDRS")
        assert casr is not None and rdrs is not None
        assert casr.product == rdrs.product == "rdrs:casr_v32"
        assert casr.grid == rdrs.grid == "projected"

    def test_coverage_declines_for_new_projected_datasets(self, tmp_path):
        """Coverage bounds drive WindowOutOfRange declines (native serves those)."""
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import WindowOutOfRange

        backend = _backend(tmp_path)
        # CONUS404 ends with WY2022; NWM3 retrospective ends Jan 2023.
        for dataset, window in (
            ("CONUS404", ("2023-06-01 00:00", "2023-06-03 00:00")),
            ("NWM3_RETROSPECTIVE", ("2024-01-01 00:00", "2024-01-02 00:00")),
        ):
            with pytest.raises(WindowOutOfRange):
                backend.acquire(_request(tmp_path, dataset=dataset, window=window))

    def test_cerra_publishes_no_wind_components(self, tmp_path):
        """CERRA ships only the combined 10m wind speed (si10), never u/v."""
        from cfs.integrations.symfluence import _spec_for

        spec = _spec_for("CERRA")
        assert spec is not None
        assert "wind_speed" in spec.variables and "wind_speed" in spec.fetchable
        assert not ({"eastward_wind", "northward_wind"} & spec.variables)

    def test_hrrr_analysis_offers_no_precipitation(self, tmp_path):
        """The hrrrzarr analysis stream has no precip on EITHER side (exp14)."""
        from cfs.integrations.symfluence import _spec_for

        spec = _spec_for("HRRR")
        assert spec is not None
        assert spec.product == "hrrr:sfc_anl"
        assert "precipitation_flux" not in spec.variables
        assert "precipitation_flux" not in spec.fetchable

    def test_daymet_offers_the_daily_derivation_trio(self, tmp_path):
        """Daymet's CFIF-declarable variables are the three daily derivations."""
        from cfs.integrations.symfluence import _spec_for

        spec = _spec_for("DAYMET")
        assert spec is not None
        assert spec.variables == {
            "air_temperature", "precipitation_flux",
            "surface_downwelling_shortwave_flux",
        }
        # dewpoint is fetchable (canonical) but not CFIF-declarable.
        assert "dewpoint_temperature" in spec.fetchable

    def test_regional_datasets_declare_spatial_domains(self, tmp_path):
        """CARRA/CERRA/HRRR/DAYMET carry a (W, S, E, N) spatial domain."""
        from cfs.integrations.symfluence import _spec_for

        assert _spec_for("CARRA").spatial == (-180.0, 55.0, 180.0, 90.0)
        assert _spec_for("CERRA").spatial == (-58.0, 20.0, 74.0, 74.0)
        assert _spec_for("HRRR").spatial == (-134.1, 21.1, -60.9, 52.6)
        assert _spec_for("DAYMET").spatial == (-179.0, 14.0, -52.0, 83.0)
        # Previously gated datasets keep their pre-spatial behaviour.
        assert _spec_for("ERA5").spatial is None
        assert _spec_for("CONUS404").spatial is None

    def test_aorc_declares_post_2002_coverage(self, tmp_path):
        pytest.importorskip("symfluence")
        caps = {c.dataset_id: c for c in _backend(tmp_path).capabilities()}
        assert caps["AORC"].temporal is not None
        assert caps["AORC"].temporal[0].startswith("2002")

    def test_interface_version_is_compatible(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.contract import is_compatible

        assert is_compatible(_backend(tmp_path).interface_version)

    def test_variables_are_cfif_names(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.preprocessing.cfif.variables import CFIF_VARIABLES

        for cap in _backend(tmp_path).capabilities():
            assert cap.variables <= set(CFIF_VARIABLES), cap.dataset_id


class TestAcquire:
    @pytest.mark.network
    def test_live_cube_to_manifest_smoke(self, tmp_path):
        """Real CFS cube traverses the SYMFLUENCE backend and manifest seam."""
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        from symfluence.data.backends.contract import MANIFEST_FILENAME, read_manifest

        backend = _backend(tmp_path)
        request = _request(
            tmp_path,
            bbox=(50.7, -114.5, 51.1, -114.0),
            window=("2015-06-01 00:00", "2015-06-01 02:00"),
        )
        result = backend.acquire(request)

        manifest = read_manifest(request.target_dir / MANIFEST_FILENAME)
        assert manifest["schema"] == "canonical-v1"
        assert manifest["backend"] == "community"
        assert manifest["paths"] == [str(result.paths[0])]
        with xr.open_dataset(result.paths[0]) as cube:
            assert cube.attrs["cfs_schema"] == "canonical-v1"
            assert cube.sizes["time"] == 3
            assert {"air_temperature", "precipitation_flux"} <= set(cube.data_vars)

    def test_acquire_writes_canonical_netcdf_and_manifest(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        from symfluence.data.backends.contract import MANIFEST_FILENAME, SchemaId, read_manifest

        import cfs

        calls = {}

        def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
            calls["product"] = product
            calls["bbox"] = bbox
            calls["variables"] = variables
            return _canonical_dataset(xr, np), _fetch_result(
                ["air_temperature", "precipitation_flux"],
                product_id="era5_arco:single_levels", provider="era5_arco",
            )

        monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)

        backend = _backend(tmp_path)
        request = _request(tmp_path)
        result = backend.acquire(request)

        assert calls["product"] == "era5_arco:single_levels"
        assert calls["bbox"] == (8.0, 46.0, 9.0, 47.0)  # (W, S, E, N)
        assert calls["variables"] is None

        assert result.backend == "community"
        assert result.schema is SchemaId.CANONICAL_V1
        assert len(result.paths) == 1 and result.paths[0].exists()
        assert result.paths[0].name == (
            "domain_test_domain_cfs_era5_arco_single_levels_20200101_20200102.nc"
        )

        manifest = read_manifest(request.target_dir / MANIFEST_FILENAME)
        assert manifest["schema"] == "canonical-v1"
        assert manifest["backend"] == "community"
        assert manifest["dataset_id"] == "ERA5"
        assert manifest["paths"] == [str(result.paths[0])]

        with xr.open_dataset(result.paths[0]) as written:
            assert written.attrs["cfs_schema"] == "canonical-v1"
            assert "air_temperature" in written.data_vars

    def test_acquire_skips_fetch_when_output_exists(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        monkeypatch.setattr(
            cfs, "fetch_sync",
            lambda *a, **k: (_canonical_dataset(xr, np), _fetch_result(["air_temperature"])),
        )
        backend = _backend(tmp_path)
        first = backend.acquire(_request(tmp_path))

        def boom(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("fetch_sync called despite existing output")

        monkeypatch.setattr(cfs, "fetch_sync", boom)
        second = backend.acquire(_request(tmp_path))
        assert second.paths == first.paths

    def test_era5_acquire_adds_native_derivations(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        ds = _canonical_dataset(xr, np)
        shape = tuple(ds.sizes[d] for d in ("time", "latitude", "longitude"))
        dims = ("time", "latitude", "longitude")
        ds["eastward_wind"] = xr.DataArray(np.full(shape, 3.0, dtype="float32"), dims=dims)
        ds["northward_wind"] = xr.DataArray(np.full(shape, 4.0, dtype="float32"), dims=dims)
        ds["dewpoint_temperature"] = xr.DataArray(np.full(shape, 270.0, dtype="float32"), dims=dims)
        ds["surface_air_pressure"] = xr.DataArray(np.full(shape, 90000.0, dtype="float32"), dims=dims)

        monkeypatch.setattr(
            cfs, "fetch_sync", lambda *a, **k: (ds, _fetch_result(["air_temperature"]))
        )
        result = _backend(tmp_path).acquire(_request(tmp_path))
        with xr.open_dataset(result.paths[0]) as written:
            assert float(written["wind_speed"].isel(time=0, latitude=0, longitude=0)) == pytest.approx(5.0)
            assert "specific_humidity" in written.data_vars

    def test_unknown_dataset_raises_dataset_unsupported(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import DatasetUnsupported

        with pytest.raises(DatasetUnsupported):
            _backend(tmp_path).acquire(_request(tmp_path, dataset="NO_SUCH"))

    def test_aorc_pre_2002_window_is_declined(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import WindowOutOfRange

        with pytest.raises(WindowOutOfRange):
            _backend(tmp_path).acquire(_request(
                tmp_path, dataset="AORC",
                window=("1999-01-01 00:00", "1999-01-03 00:00"),
            ))

    def test_out_of_domain_bbox_is_refused(self, tmp_path):
        """Regional datasets refuse out-of-domain bboxes with a clear error.

        This is NOT a decline-to-native: the data does not exist there on any
        backend, so a plain AcquisitionError (not WindowOutOfRange /
        DatasetUnsupported) is raised.
        """
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import AcquisitionError

        backend = _backend(tmp_path)
        # Default test bbox is the Alps (46-47N, 8-9E): south of CARRA's
        # Arctic domain, outside HRRR's and Daymet's longitudes.
        for dataset in ("CARRA", "HRRR", "DAYMET"):
            with pytest.raises(AcquisitionError, match="spatial domain"):
                backend.acquire(_request(
                    tmp_path, dataset=dataset,
                    window=("2018-06-01 00:00", "2018-06-03 00:00"),
                ))
        # CERRA with a CONUS bbox (west of the CERRA domain).
        with pytest.raises(AcquisitionError, match="spatial domain"):
            backend.acquire(_request(
                tmp_path, dataset="CERRA",
                bbox=(39.5, -105.5, 40.0, -105.0),
                window=("2018-06-01 00:00", "2018-06-03 00:00"),
            ))

    def test_carra_domain_key_reaches_the_connector(self, tmp_path, monkeypatch):
        """The native CARRA_DOMAIN key selects the CDS Arctic domain split."""
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        seen = {}

        def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
            seen["product"] = product
            seen["config"] = config
            return _canonical_dataset(xr, np), _fetch_result(
                ["air_temperature", "precipitation_flux"], product_id=product
            )

        monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)
        backend = _backend(tmp_path, CARRA_DOMAIN="east_domain")
        backend.acquire(_request(
            tmp_path, dataset="CARRA",
            bbox=(67.0, 18.0, 68.0, 20.0),  # Scandinavia (inside the Arctic domain)
            window=("2018-06-01 00:00", "2018-06-03 00:00"),
        ))
        assert seen["product"] == "carra:single_levels"
        assert seen["config"] == {"domain": "east_domain"}

    def test_missing_window_is_an_acquisition_error(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import AcquisitionError

        with pytest.raises(AcquisitionError, match="explicit"):
            _backend(tmp_path).acquire(_request(tmp_path, window=None))

    def test_cfs_errors_map_onto_protocol_taxonomy(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import AuthRequired, UpstreamOutage

        import cfs
        from cfs.core.exceptions import ConnectorError, RegistrationRequiredError

        def raise_auth(*a, **k):
            raise RegistrationRequiredError("nldas", "https://urs.earthdata.nasa.gov", "make an account")

        monkeypatch.setattr(cfs, "fetch_sync", raise_auth)
        with pytest.raises(AuthRequired) as excinfo:
            _backend(tmp_path).acquire(_request(tmp_path, dataset="NLDAS"))
        assert excinfo.value.provider == "nldas"
        assert "account" in excinfo.value.howto

        def raise_outage(*a, **k):
            raise ConnectorError("era5_arco", "store unreachable")

        monkeypatch.setattr(cfs, "fetch_sync", raise_outage)
        with pytest.raises(UpstreamOutage):
            _backend(tmp_path).acquire(_request(tmp_path))

    def test_nldas_legacy_variables_key_translates_native_names(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        calls = {}

        def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
            calls["product"] = product
            calls["variables"] = variables
            return _canonical_dataset(xr, np), _fetch_result(["air_temperature"])

        monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)
        backend = _backend(tmp_path, NLDAS_VARIABLES=["Tair", "Rainf", "CAPE"])
        backend.acquire(_request(tmp_path, dataset="NLDAS"))

        assert calls["product"] == "nldas:fora0125_h"
        assert calls["variables"] == ["air_temperature", "precipitation_flux"]  # CAPE dropped, loudly

    def test_requested_wind_speed_fetches_primitives_for_rdrs(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        calls = {}

        def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
            calls["product"] = product
            calls["variables"] = variables
            return _projected_canonical_dataset(xr, np, n_times=4), _fetch_result(
                ["eastward_wind"], product_id="rdrs:casr_v32", provider="rdrs")

        monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)
        _backend(tmp_path).acquire(_request(
            tmp_path, dataset="RDRS",
            window=("2018-06-01 00:00", "2018-06-03 00:00"),
            variables=frozenset({"wind_speed"}),
        ))
        assert calls["product"] == "rdrs:casr_v32"
        assert set(calls["variables"]) == {"eastward_wind", "northward_wind"}


class TestNexGddp:
    def test_builds_product_from_native_keys_and_clips_window(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        calls = []

        def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
            calls.append({"product": product, "time_range": time_range, "config": config})
            return _canonical_dataset(xr, np), _fetch_result(["air_temperature"])

        monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)
        backend = _backend(
            tmp_path,
            NEX_MODELS=["ACCESS-CM2"],
            NEX_SCENARIOS=["historical"],
            NEX_ENSEMBLES=["r1i1p1f1"],
        )
        result = backend.acquire(_request(
            tmp_path, dataset="NEX-GDDP",
            window=("2010-01-01 00:00", "2020-01-01 00:00"),
        ))

        assert len(calls) == 1
        assert calls[0]["product"] == "nex_gddp:historical"
        assert calls[0]["config"] == {"model": "ACCESS-CM2", "member": "r1i1p1f1"}
        assert calls[0]["time_range"][1].year == 2014  # clipped to the scenario extent

        assert len(result.paths) == 1
        with xr.open_dataset(result.paths[0]) as written:
            # Synthetic pressure mirrors the native handler (sea-level constant).
            assert float(written["surface_air_pressure"].max()) == pytest.approx(101325.0)

    def test_requires_models(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import AcquisitionError

        with pytest.raises(AcquisitionError, match="NEX_MODELS"):
            _backend(tmp_path).acquire(_request(tmp_path, dataset="NEX-GDDP"))


class TestParallelNameCFS:
    def test_product_comes_from_options_or_config(self, tmp_path, monkeypatch):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        import cfs

        calls = {}

        def fake_fetch_sync(product, bbox, time_range, variables=None, config=None):
            calls["product"] = product
            calls["config"] = config
            return _canonical_dataset(xr, np), _fetch_result(["air_temperature"])

        monkeypatch.setattr(cfs, "fetch_sync", fake_fetch_sync)
        _backend(tmp_path).acquire(_request(
            tmp_path, dataset="CFS",
            options={"product": "gefs:atmos_0p25", "connector_config": {"members": ["gec00"]}},
        ))
        assert calls["product"] == "gefs:atmos_0p25"
        assert calls["config"] == {"members": ["gec00"]}

        calls.clear()
        backend = _backend(tmp_path, CFS_PRODUCT="chirps:daily")
        backend.acquire(_request(tmp_path, dataset="CFS", target_dir=tmp_path / "raw2"))
        assert calls["product"] == "chirps:daily"

    def test_missing_product_is_an_acquisition_error(self, tmp_path):
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import AcquisitionError

        with pytest.raises(AcquisitionError, match="CFS_PRODUCT"):
            _backend(tmp_path).acquire(_request(tmp_path, dataset="CFS"))

    def test_framework_refuses_ungated_cfs_without_optin(self, tmp_path):
        """The parallel-name entry exercises the framework's ungated policy."""
        pytest.importorskip("symfluence")
        from symfluence.data.backends.errors import DatasetUnsupported
        from symfluence.data.backends.selection import select_backend

        from cfs.integrations.symfluence import register

        register()
        config = _make_symfluence_config(tmp_path, DATA_ACCESS="community")
        # 'CFS' is claimed only by the community backend, with parity None:
        # refused without the opt-in, and nothing else can serve it.
        with pytest.raises(DatasetUnsupported, match="ungraded|ALLOW_UNGATED"):
            select_backend("CFS", config, logger=logging.getLogger("test_cfs"))

        config["ALLOW_UNGATED_BACKENDS"] = True
        backend = select_backend("CFS", config, logger=logging.getLogger("test_cfs"))
        assert backend.name == "community"

    def test_framework_routes_rdrs_to_community(self, tmp_path):
        """Selection smoke: graded RDRS resolves to the community backend."""
        pytest.importorskip("symfluence")
        from symfluence.data.backends.selection import select_backend

        from cfs.integrations.symfluence import register

        register()
        config = _make_symfluence_config(tmp_path, DATA_ACCESS="community")
        backend = select_backend(
            "RDRS", config,
            window=("2018-06-01 00:00", "2018-06-04 00:00"),
            logger=logging.getLogger("test_cfs"),
        )
        assert backend.name == "community"

    def test_framework_routes_newly_gated_datasets_to_community(self, tmp_path):
        """Selection smoke for the 2026-06-12b additions (graded -> community)."""
        pytest.importorskip("symfluence")
        from symfluence.data.backends.selection import select_backend

        from cfs.integrations.symfluence import register

        register()
        config = _make_symfluence_config(tmp_path, DATA_ACCESS="community")
        for dataset in ("CARRA", "CERRA", "HRRR", "DAYMET"):
            backend = select_backend(
                dataset, config,
                window=("2018-06-01 00:00", "2018-06-04 00:00"),
                logger=logging.getLogger("test_cfs"),
            )
            assert backend.name == "community", dataset


# ── Tier 4: the schema-keyed canonical-v1 dataset handler ───────────


def _handler(tmp_path, **extra):
    from cfs.integrations.symfluence import CanonicalV1Handler

    return CanonicalV1Handler(
        _make_symfluence_config(tmp_path, **extra), logging.getLogger("test_cfs"), tmp_path
    )


class TestCanonicalHandlerRegularGrid:
    def test_contract_methods(self, tmp_path):
        pytest.importorskip("symfluence")
        handler = _handler(tmp_path)
        assert handler.get_coordinate_names() == ("latitude", "longitude")
        assert handler.needs_merging() is True
        mapping = handler.get_variable_mapping()
        assert mapping == {k: k for k in mapping}

    def test_process_dataset_applies_cfif_attributes(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        out = _handler(tmp_path).process_dataset(_canonical_dataset(xr, np))
        assert out["air_temperature"].attrs["units"] == "K"
        assert out["precipitation_flux"].attrs["units"] == "kg m-2 s-1"
        assert out["precipitation_flux"].attrs["standard_name"] == "precipitation_rate"

    def test_merged_file_pattern_uses_forcing_dataset_prefix(self, tmp_path):
        pytest.importorskip("symfluence")
        handler = _handler(tmp_path, FORCING_DATASET="RDRS")
        assert handler.get_merged_file_pattern(2018, 6) == "RDRS_monthly_201806.nc"

    def test_three_hourly_regular_store_keeps_its_time_axis(self, tmp_path):
        """CARRA/CERRA-class quirk: 3-hourly REGULAR grids pass the per-file
        regular merge loop untouched (no hourly inflation, wind_speed derived)."""
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")
        import pandas as pd

        raw = tmp_path / "raw_data"
        merged = tmp_path / "merged_path"
        raw.mkdir()
        ds = _canonical_dataset(xr, np)
        times = pd.date_range("2018-06-01", periods=16, freq="3h")  # 2 days, 3-hourly
        shape = (len(times), ds.sizes["latitude"], ds.sizes["longitude"])
        ds = ds.isel(time=0, drop=True).expand_dims(time=times).transpose(
            "time", "latitude", "longitude"
        ).copy()
        ds["eastward_wind"] = (
            ("time", "latitude", "longitude"),
            np.full(shape, 3.0, dtype="float32"),
            {"standard_name": "eastward_wind", "units": "m s-1"},
        )
        ds["northward_wind"] = (
            ("time", "latitude", "longitude"),
            np.full(shape, 4.0, dtype="float32"),
            {"standard_name": "northward_wind", "units": "m s-1"},
        )
        name = "domain_test_domain_cfs_carra_single_levels_20180601_20180602.nc"
        ds.to_netcdf(raw / name)

        handler = _handler(tmp_path, FORCING_DATASET="CARRA")
        handler.merge_forcings(raw, merged, 2018, 2018)

        out = merged / name  # regular path keeps the raw filename
        assert out.exists(), sorted(p.name for p in merged.iterdir())
        with xr.open_dataset(out) as got:
            assert got.sizes["time"] == 16  # 3-hourly axis NOT inflated
            step = np.diff(got["time"].values).astype("timedelta64[h]")
            assert (step == np.timedelta64(3, "h")).all()
            assert "wind_speed" in got.data_vars
            assert float(got["wind_speed"].isel(time=0, latitude=0, longitude=0)) == pytest.approx(5.0)


class TestCanonicalHandlerProjectedGrid:
    def test_process_dataset_handles_projected_and_derives_wind_speed(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        ds = _projected_canonical_dataset(xr, np, n_times=4)
        out = _handler(tmp_path).process_dataset(ds)

        # No NotImplementedError (the v1 regular-grid-only limitation is gone),
        # wind_speed derived as hypot(u, v) with the native float32 op order.
        assert "wind_speed" in out.data_vars
        assert float(out["wind_speed"].isel(time=0, rlat=0, rlon=0)) == pytest.approx(5.0)
        assert out["wind_speed"].attrs["units"] == "m s-1"
        # SI canonical values untouched (no native unit heuristics ran).
        assert float(out["air_temperature"].max()) == pytest.approx(280.0)
        assert float(out["surface_air_pressure"].max()) == pytest.approx(90000.0)

    def test_merge_forcings_splits_consolidated_store_monthly(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        raw = tmp_path / "raw_data"
        merged = tmp_path / "merged_path"
        raw.mkdir()
        ds = _projected_canonical_dataset(xr, np, n_times=72)  # 3 days, hourly
        ds.to_netcdf(raw / "domain_test_domain_cfs_rdrs_casr_v32_20180601_20180603.nc")

        handler = _handler(tmp_path, FORCING_DATASET="RDRS")
        handler.merge_forcings(raw, merged, 2018, 2018)

        out = merged / "RDRS_monthly_201806.nc"
        assert out.exists(), sorted(p.name for p in merged.iterdir())
        with xr.open_dataset(out) as monthly:
            # Native-pipeline shape: full hourly month, projected dims intact.
            assert monthly.sizes["time"] == 30 * 24
            assert monthly.sizes["rlat"] == 4 and monthly.sizes["rlon"] == 5
            assert "wind_speed" in monthly.data_vars
            assert monthly["latitude"].ndim == 2
            # Gap-fill (ffill/bfill) leaves no NaNs.
            assert not bool(np.isnan(monthly["air_temperature"].values).any())

    def test_create_shapefile_builds_per_cell_polygons(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")
        gpd = pytest.importorskip("geopandas")

        merged = tmp_path / "merged_path"
        merged.mkdir()
        ds = _projected_canonical_dataset(xr, np, n_times=4)
        ds.to_netcdf(merged / "RDRS_monthly_201806.nc")

        handler = _handler(tmp_path, FORCING_DATASET="RDRS")
        shp_dir = tmp_path / "shapefiles"
        shp_dir.mkdir()
        out = handler.create_shapefile(
            shp_dir, merged, tmp_path / "dem.tif",
            lambda gdf, dem_path, batch_size=50: [123.0] * len(gdf),
        )

        gdf = gpd.read_file(out)
        assert len(gdf) == 4 * 5  # one polygon per native grid cell
        assert set(gdf.columns) >= {"ID", "lat", "lon", "elev_m", "geometry"}
        # Interior cells are valid quads; the last row/column are the same
        # degenerate edge cells the native shapefile carries (exact port).
        assert int(gdf.geometry.is_valid.sum()) == (4 - 1) * (5 - 1)
        assert (gdf["elev_m"] == 123.0).all()
        # Cell corners come from the 2-D coordinate arrays.
        assert gdf.total_bounds[0] == pytest.approx(float(ds["longitude"].min()), abs=1e-5)

    @pytest.mark.skipif(not EXP10_ARTIFACT.exists(), reason="exp10 artifact not present")
    def test_exp10_artifact_processes_through_the_handler(self, tmp_path):
        """The real bit-identical RDRS canonical store (exp10) round-trips."""
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        handler = _handler(tmp_path, FORCING_DATASET="RDRS")
        with xr.open_dataset(EXP10_ARTIFACT) as ds:
            assert handler._is_projected(ds)
            out = handler.process_dataset(ds.load())

        # All nine canonical variables present under CFIF names + derived wind.
        expected = {
            "air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux",
            "wind_speed", "dewpoint_temperature",
        }
        assert expected <= set(out.data_vars)
        # wind_speed = hypot(u, v), exp10-documented <= 9e-4 m/s vs CaSR sfcWind.
        u = out["eastward_wind"].values
        v = out["northward_wind"].values
        np.testing.assert_array_equal(
            out["wind_speed"].values,
            ((out["eastward_wind"] ** 2 + out["northward_wind"] ** 2) ** 0.5)
            .astype("float32").values,
        )
        assert np.isfinite(u).all() and np.isfinite(v).all()
        assert out["latitude"].ndim == 2 and out["longitude"].ndim == 2

    @pytest.mark.skipif(not EXP10_ARTIFACT.exists(), reason="exp10 artifact not present")
    def test_exp10_artifact_shapefile(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        gpd = pytest.importorskip("geopandas")
        import shutil

        merged = tmp_path / "merged_path"
        merged.mkdir()
        shutil.copy(EXP10_ARTIFACT, merged / "RDRS_monthly_201806.nc")

        handler = _handler(tmp_path, FORCING_DATASET="RDRS")
        out = handler.create_shapefile(
            tmp_path, merged, tmp_path / "dem.tif",
            lambda gdf, dem_path, batch_size=50: [0.0] * len(gdf),
        )
        gdf = gpd.read_file(out)
        with xr.open_dataset(EXP10_ARTIFACT) as ds:
            n_y, n_x = ds.sizes["rlat"], ds.sizes["rlon"]
        assert len(gdf) == n_y * n_x  # 9x9 exp10 grid
        # Interior quads valid; degenerate edge cells match the native port.
        assert int(gdf.geometry.is_valid.sum()) == (n_y - 1) * (n_x - 1)


class TestCanonicalHandlerLCCGrid:
    """The second projected family: LCC y/x dims (CONUS404 / HRRR / NWM3)."""

    def test_lcc_layout_detected_as_projected(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        handler = _handler(tmp_path)
        assert handler._is_projected(_lcc_canonical_dataset(xr, np, n_times=4))

    def test_process_dataset_derives_wind_speed_on_lcc(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        out = _handler(tmp_path).process_dataset(_lcc_canonical_dataset(xr, np, n_times=4))
        assert "wind_speed" in out.data_vars
        assert float(out["wind_speed"].isel(time=0, y=0, x=0)) == pytest.approx(5.0)
        assert out["air_temperature"].attrs["units"] == "K"

    def test_merge_forcings_splits_lcc_store_monthly(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        raw = tmp_path / "raw_data"
        merged = tmp_path / "merged_path"
        raw.mkdir()
        ds = _lcc_canonical_dataset(xr, np, n_times=72)  # 3 days hourly
        ds.to_netcdf(raw / "domain_test_domain_cfs_conus404_hourly_20180601_20180603.nc")

        handler = _handler(tmp_path, FORCING_DATASET="CONUS404")
        handler.merge_forcings(raw, merged, 2018, 2018)

        out = merged / "CONUS404_monthly_201806.nc"
        assert out.exists(), sorted(p.name for p in merged.iterdir())
        with xr.open_dataset(out) as monthly:
            # Hourly store keeps the exact native-pipeline shape: full month.
            assert monthly.sizes["time"] == 30 * 24
            assert monthly.sizes["y"] == 4 and monthly.sizes["x"] == 5
            assert monthly["latitude"].ndim == 2
            assert "wind_speed" in monthly.data_vars
            assert not bool(np.isnan(monthly["air_temperature"].values).any())

    def test_create_shapefile_builds_per_cell_polygons_on_lcc(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")
        gpd = pytest.importorskip("geopandas")

        merged = tmp_path / "merged_path"
        merged.mkdir()
        ds = _lcc_canonical_dataset(xr, np, n_times=4)
        ds.to_netcdf(merged / "CONUS404_monthly_201806.nc")

        handler = _handler(tmp_path, FORCING_DATASET="CONUS404")
        shp_dir = tmp_path / "shapefiles"
        shp_dir.mkdir()
        out = handler.create_shapefile(
            shp_dir, merged, tmp_path / "dem.tif",
            lambda gdf, dem_path, batch_size=50: [1600.0] * len(gdf),
        )

        gdf = gpd.read_file(out)
        assert len(gdf) == 4 * 5
        # Interior cells valid quads from the 2-D lat/lon corners; the last
        # row/column are the same degenerate edge cells as the native port.
        assert int(gdf.geometry.is_valid.sum()) == (4 - 1) * (5 - 1)
        assert gdf.total_bounds[0] == pytest.approx(float(ds["longitude"].min()), abs=1e-5)


class TestCanonicalHandlerDaymetGrid:
    """The daily LCC family (Daymet): same grid class, non-hourly time axis."""

    def test_daily_store_is_not_inflated_to_hourly(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        raw = tmp_path / "raw_data"
        merged = tmp_path / "merged_path"
        raw.mkdir()
        # 14 daily steps anchored at noon (Daymet convention), June 2018.
        ds = _lcc_canonical_dataset(xr, np, n_times=14, freq="D")
        ds.to_netcdf(raw / "domain_test_domain_cfs_daymet_daily_v4_20180601_20180614.nc")

        handler = _handler(tmp_path, FORCING_DATASET="DAYMET")
        handler.merge_forcings(raw, merged, 2018, 2018)

        out = merged / "DAYMET_monthly_201806.nc"
        assert out.exists(), sorted(p.name for p in merged.iterdir())
        with xr.open_dataset(out) as monthly:
            # Daily axis preserved (no hourly reindex/interpolation blow-up)
            # and the noon anchoring of the store kept.
            assert monthly.sizes["time"] == 14
            import pandas as pd

            stamps = pd.to_datetime(monthly["time"].values)
            assert (stamps.hour == 12).all()
            assert not bool(np.isnan(monthly["air_temperature"].values).any())

    def test_daily_store_gap_is_filled_at_native_step(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")

        raw = tmp_path / "raw_data"
        merged = tmp_path / "merged_path"
        raw.mkdir()
        ds = _lcc_canonical_dataset(xr, np, n_times=10, freq="D")
        ds = ds.isel(time=[0, 1, 2, 4, 5, 6, 7, 8, 9])  # drop day 4 (gap)
        ds.to_netcdf(raw / "domain_test_domain_cfs_daymet_daily_v4_20180601_20180610.nc")

        handler = _handler(tmp_path, FORCING_DATASET="DAYMET")
        handler.merge_forcings(raw, merged, 2018, 2018)

        with xr.open_dataset(merged / "DAYMET_monthly_201806.nc") as monthly:
            assert monthly.sizes["time"] == 10  # gap restored at the daily step
            assert not bool(np.isnan(monthly["air_temperature"].values).any())

    def test_native_time_step_inference(self, tmp_path):
        pytest.importorskip("symfluence")
        xr = pytest.importorskip("xarray")
        np = pytest.importorskip("numpy")
        import pandas as pd

        handler = _handler(tmp_path)
        hourly = _lcc_canonical_dataset(xr, np, n_times=5)
        daily = _lcc_canonical_dataset(xr, np, n_times=5, freq="D")
        assert handler._native_time_step(hourly) == pd.Timedelta(hours=1)
        assert handler._native_time_step(daily) == pd.Timedelta(days=1)
        assert handler._native_time_step(hourly.isel(time=[0])) is None
