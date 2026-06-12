# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""SYMFLUENCE integration — CFS as a drop-in forcing backend + plugin.

This module wires CFS into SYMFLUENCE through the ``symfluence.plugins``
entry-point group (see ``pyproject.toml``): ``import symfluence`` discovers the
entry point and calls :func:`register`. Two modes coexist:

**1. Drop-in shadow mode (the primary mode).** For every *parity-validated*
native forcing dataset (see :data:`SHADOW_SPECS`), :func:`register` captures
the native SYMFLUENCE acquisition class from ``R.acquisition_handlers`` and
re-registers a shadow wrapper under the **same name** (SYMFLUENCE registers
its in-tree handlers before plugin discovery runs, and ``Registry.add``
overwrites silently — verified empirically, 2026-06-11). Users keep their
existing configs (``FORCING_DATASET: ERA5`` etc.); the wrapper routes at
``download()`` time:

* ``DATA_ACCESS: community`` → acquire via :func:`cfs.fetch_sync` and write
  canonical-v1 NetCDF (global attr ``cfs_schema`` is the downstream detection
  key).
* anything else (default ``MAF``, or ``cloud``) → instantiate the captured
  native class and delegate — **exactly** the native code runs, bit-identical
  to an installation without this plugin.
* per-dataset opt-out: a flat ``<NATIVE_NAME>_BACKEND: native`` key (e.g.
  ``ERA5_BACKEND: native``) wins over the global ``DATA_ACCESS`` value.

Matching **self-detecting dataset-handler shadows** are registered in
``R.dataset_handlers``: each method that touches raw files first checks the
``cfs_schema`` attribute (canonical-v1 → the CFS canonical→CFIF path; native
format → delegate to the captured native dataset handler), so a domain can
even mix cached native raw files with newly community-acquired ones.

**2. Parallel-name mode** (kept from 0.3.0). ``FORCING_DATASET: CFS`` +
``CFS_PRODUCT: <provider:product>`` exposes the *whole* CFS catalog —
including products with no SYMFLUENCE equivalent (GEFS, GFS, MERRA2, CHIRPS,
GridMET, …) — via :class:`CFSForcingAcquirer` / :class:`CFSDatasetHandler`.

The module is intentionally decoupled (same pattern as climaclass's
SYMFLUENCE integration):

* SYMFLUENCE base classes are resolved defensively at import time; if
  SYMFLUENCE is absent the bases degrade to ``object`` so ``import cfs`` (and
  ``import cfs.integrations.symfluence``) never fails.
* :func:`register` *does* import SYMFLUENCE — when it is absent the resulting
  ``ImportError`` is exactly what SYMFLUENCE's plugin discovery expects and
  silently skips.
* CFS itself is imported lazily inside methods (house style for heavy deps).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

if TYPE_CHECKING:
    import xarray as xr

# Resolve the SYMFLUENCE base classes defensively so importing this module
# never hard-fails when SYMFLUENCE is not installed.
try:  # pragma: no cover - exercised only with SYMFLUENCE present
    from symfluence.data.acquisition.base import (
        BaseAcquisitionHandler as _AcquisitionBase,
    )
    from symfluence.data.preprocessing.dataset_handlers.base_dataset import (
        BaseDatasetHandler as _DatasetBase,
    )

    HAVE_SYMFLUENCE = True
except Exception:  # noqa: BLE001 - any import failure means "not available"
    _AcquisitionBase = object  # type: ignore[assignment, misc]
    _DatasetBase = object  # type: ignore[assignment, misc]
    HAVE_SYMFLUENCE = False


#: CFS canonical-v1 variable names -> SYMFLUENCE CFIF names.
#:
#: Both vocabularies use CF-aligned standard names in SI units, so every CFS
#: canonical variable that CFIF defines maps by *identity* (compare
#: ``cfs.core.vocabulary.CanonicalVar`` with
#: ``symfluence.data.preprocessing.cfif.variables.CFIF_VARIABLES``).
#: ``dewpoint_temperature`` is the one CFS canonical variable without a CFIF
#: counterpart; it is deliberately absent here and passes through unchanged
#: (its name is already a CF standard name, and CFIF consumers ignore
#: variables they do not know).
CFS_TO_CFIF_MAPPING: dict[str, str] = {
    "air_temperature": "air_temperature",
    "specific_humidity": "specific_humidity",
    "precipitation_flux": "precipitation_flux",
    "eastward_wind": "eastward_wind",
    "northward_wind": "northward_wind",
    "wind_speed": "wind_speed",
    "surface_air_pressure": "surface_air_pressure",
    "surface_downwelling_shortwave_flux": "surface_downwelling_shortwave_flux",
    "surface_downwelling_longwave_flux": "surface_downwelling_longwave_flux",
}


# ── The shadow map ───────────────────────────────────────────────────


class ShadowSpec(NamedTuple):
    """One parity-validated native dataset shadowed by a CFS backend.

    Attributes:
        family: Human-readable dataset family name (used in logs/errors and
            to derive shadow class names).
        aliases: Every name the native acquisition handler registers under in
            ``R.acquisition_handlers`` — the wrapper shadows all of them.
        dataset_keys: The ``R.dataset_handlers`` keys to shadow with the
            self-detecting preprocessing handler.
        product: Fixed CFS product id for the community fetch, or ``None``
            for NEX-GDDP where the product is built per scenario
            (``nex_gddp:<scenario>``) from the native config keys.
        variables_key: Flat config key holding the native variable list
            (``None`` → always fetch all canonical variables the product
            offers).
        native_to_canonical: Native variable name → canonical-v1 name map
            used to translate ``variables_key`` requests.
        parity: Live-measured parity grade vs the native pipeline
            (2026-06-11 experiments; see docs/symfluence.md).
    """

    family: str
    aliases: tuple[str, ...]
    dataset_keys: tuple[str, ...]
    product: str | None
    variables_key: str | None
    native_to_canonical: dict[str, str] | None
    parity: str


#: NLDAS-2 v2.0 NetCDF (ALMA) names -> canonical-v1, mirroring the CFS
#: ``nldas`` connector's source mapping (connectors/nldas.py).
_NLDAS_TO_CANONICAL: dict[str, str] = {
    "Tair": "air_temperature",
    "Qair": "specific_humidity",
    "PSurf": "surface_air_pressure",
    "Wind_E": "eastward_wind",
    "Wind_N": "northward_wind",
    "LWdown": "surface_downwelling_longwave_flux",
    "SWdown": "surface_downwelling_shortwave_flux",
    "Rainf": "precipitation_flux",
}

#: NEX-GDDP-CMIP6 native names -> canonical-v1 (connectors/nex_gddp.py).
#: ``hurs``/``tasmax``/``tasmin`` have no canonical-v1 counterpart and are
#: skipped (loudly) when requested via NEX_VARIABLES.
_NEX_TO_CANONICAL: dict[str, str] = {
    "tas": "air_temperature",
    "pr": "precipitation_flux",
    "huss": "specific_humidity",
    "sfcWind": "wind_speed",
    "rsds": "surface_downwelling_shortwave_flux",
    "rlds": "surface_downwelling_longwave_flux",
}

#: Parity-gated shadow map: ONLY datasets whose native-vs-community output was
#: live-validated (2026-06-11) are shadowed. MSWEP and EM-EARTH are
#: deliberately EXCLUDED until live parity validation is possible (blocked:
#: no rclone Drive remote / the EM-Earth S3 bucket denies anonymous GET) —
#: their native handlers keep running untouched under every DATA_ACCESS value.
#: Projected/curvilinear-grid datasets (CARRA, CERRA, RDRS, CONUS404, HRRR,
#: DAYMET, NWM3_RETROSPECTIVE, CASR) are likewise not shadowed in v1.
SHADOW_SPECS: tuple[ShadowSpec, ...] = (
    ShadowSpec(
        family="ERA5",
        aliases=("ERA5",),
        dataset_keys=("era5",),  # 'era5_cds' stays native (separate CDS-raw layout)
        product="era5_arco:single_levels",
        variables_key=None,
        native_to_canonical=None,
        parity="value-identical; 3 accumulation->flux vars differ <= 2 float32 ulps (op-order only)",
    ),
    ShadowSpec(
        family="NLDAS",
        aliases=("NLDAS", "NLDAS2", "NLDAS-2"),
        # SYMFLUENCE has NO native NLDAS dataset handler (only a variable
        # rename map in data/utils/variable_utils.py), so these keys are
        # registered outright for community-acquired canonical files only.
        dataset_keys=("nldas", "nldas2", "nldas-2"),
        product="nldas:fora0125_h",
        variables_key="NLDAS_VARIABLES",
        native_to_canonical=_NLDAS_TO_CANONICAL,
        parity="bitwise-identical (7/8 vars); precip <= 1 float32 ulp",
    ),
    ShadowSpec(
        family="AORC",
        aliases=("AORC",),
        dataset_keys=("aorc",),
        product="aorc:conus_1km",
        variables_key=None,
        native_to_canonical=None,
        parity="bit-identical",
    ),
    ShadowSpec(
        family="NEX-GDDP",
        aliases=("NEX-GDDP-CMIP6", "NEX-GDDP"),
        dataset_keys=("nex-gddp-cmip6", "nex-gddp"),
        product=None,  # nex_gddp:<scenario>, built from NEX_* config keys
        variables_key="NEX_VARIABLES",
        native_to_canonical=_NEX_TO_CANONICAL,
        parity="bit-identical (same physical files; NCCS THREDDS vs S3 mirror)",
    ),
)


def _backend_keys(spec: ShadowSpec) -> tuple[str, ...]:
    """Flat per-dataset opt-out keys, one per registry alias.

    ``ERA5`` → ``('ERA5_BACKEND',)``; ``NLDAS``/``NLDAS2``/``NLDAS-2`` →
    ``('NLDAS_BACKEND', 'NLDAS2_BACKEND', 'NLDAS_2_BACKEND')``.
    """
    keys: list[str] = []
    for alias in spec.aliases:
        key = re.sub(r"[^A-Za-z0-9]+", "_", alias).strip("_").upper() + "_BACKEND"
        if key not in keys:
            keys.append(key)
    return tuple(keys)


# ── Shared helpers ───────────────────────────────────────────────────


def _netcdf_encoding(ds: xr.Dataset) -> dict[str, dict[str, Any]]:
    """Compressed per-variable encoding for ``ds.to_netcdf(encoding=...)``.

    Mirrors SYMFLUENCE's ``ChunkedDownloadMixin.get_netcdf_encoding`` defaults
    (zlib, complevel 1) without requiring the mixin, so the acquirer keeps a
    single defensive base class.
    """
    return {str(name): {"zlib": True, "complevel": 1} for name in ds.data_vars}


def _require_regular_grid(ds: xr.Dataset) -> None:
    """Raise for projected/curvilinear canonical datasets (unsupported in v1).

    Per the canonical-v1 spec, regular grids carry 1-D ``latitude`` /
    ``longitude`` *dimension* coordinates; projected products (rotated-pole or
    LCC: rdrs, conus404, hrrr, daymet, narr, aorc_nwm, nwm_operational) keep
    native ``rlat``/``rlon`` or ``y``/``x`` dims with 2-D lat/lon auxiliaries.
    """
    if "latitude" in ds.dims and "longitude" in ds.dims:
        return
    raise NotImplementedError(
        "The CFS dataset handler supports regular latitude/longitude grids only (v1). "
        f"This dataset has dims {tuple(ds.dims)}, i.e. a projected/curvilinear product "
        "(native rlat/rlon or y/x dims with 2-D latitude/longitude coordinates per "
        "canonical-v1). Pick a regular-grid CFS product (see the CFS catalog 'grid' "
        "column) or open an issue if you need projected-grid support."
    )


def _bbox_tuple(handler: Any) -> tuple[float, float, float, float]:
    """SYMFLUENCE bbox dict -> CFS ``(min_lon, min_lat, max_lon, max_lat)``."""
    if not handler.bbox:
        raise ValueError(
            "No bounding box available for the CFS acquisition handler. "
            "Set BOUNDING_BOX_COORDS ('north/west/south/east') in your configuration."
        )
    return (
        float(handler.bbox["lon_min"]),
        float(handler.bbox["lat_min"]),
        float(handler.bbox["lon_max"]),
        float(handler.bbox["lat_max"]),
    )


def _product_tag(product: str) -> str:
    """Filesystem-safe tag for a CFS product id (``aorc:conus_1km`` → ``aorc_conus_1km``)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", product).strip("_").lower()


def _community_output_path(handler: Any, output_dir: Path, tag: str) -> Path:
    """Canonical output filename: ``domain_{name}_cfs_{tag}_{start}_{end}.nc``."""
    start_str = handler.start_date.strftime("%Y%m%d")
    end_str = handler.end_date.strftime("%Y%m%d")
    return Path(output_dir) / f"domain_{handler.domain_name}_cfs_{tag}_{start_str}_{end_str}.nc"


def _canonical_to_cfif(handler: Any, ds: xr.Dataset) -> xr.Dataset:
    """Rename a canonical-v1 dataset to CFIF and apply standard attributes.

    Data arrive in canonical SI units identical to CFIF's (K, kg m-2 s-1,
    W m-2, Pa, m s-1), so no unit conversion is performed. Shared by
    :class:`CFSDatasetHandler` and the self-detecting shadow handlers (which
    must not consult their verdict-dependent ``get_variable_mapping``).
    """
    _require_regular_grid(ds)
    renames = {
        old: new for old, new in CFS_TO_CFIF_MAPPING.items() if old in ds.variables and new != old
    }
    if renames:  # pragma: no cover - identity mapping today; future-proofing
        ds = ds.rename(renames)
    return cast("xr.Dataset", handler.apply_standard_attributes(ds))


def _add_native_era5_derivations(ds: xr.Dataset) -> xr.Dataset:
    """Derive ``wind_speed`` / ``specific_humidity`` exactly like native SYMFLUENCE.

    The native ERA5 acquisition pipeline (``handlers/era5_processing.py``)
    ships these two derived variables in its raw files; CFS's ``era5_arco``
    product ships only the primitives (u/v wind, dewpoint, pressure). To keep
    the community files drop-in equivalent, the shadow recomputes them with
    the **same float32 op order as SYMFLUENCE** — live-verified bitwise equal
    to the native output (2026-06-11 parity experiments). Do not "improve"
    these formulas: bit-parity with the native pipeline is the contract.
    """
    import numpy as np
    import xarray

    if "wind_speed" not in ds.data_vars and {"eastward_wind", "northward_wind"} <= set(ds.data_vars):
        u, v = ds["eastward_wind"], ds["northward_wind"]
        wind = ((u**2 + v**2) ** 0.5).astype("float32")
        wind.attrs = {"units": "m s-1", "long_name": "wind speed", "standard_name": "wind_speed"}
        ds["wind_speed"] = wind
    if "specific_humidity" not in ds.data_vars and {
        "dewpoint_temperature",
        "surface_air_pressure",
    } <= set(ds.data_vars):
        td_c = ds["dewpoint_temperature"] - 273.15
        es = 611.2 * np.exp((17.67 * td_c) / (td_c + 243.5))
        pressure = ds["surface_air_pressure"]
        denom = xarray.where((pressure - es) <= 1.0, 1.0, pressure - es)
        r = 0.622 * es / denom
        q = (r / (1.0 + r)).astype("float32")
        q.attrs = {"units": "kg kg-1", "long_name": "specific humidity", "standard_name": "specific_humidity"}
        ds["specific_humidity"] = q
    return ds


# ── Parallel-name mode (FORCING_DATASET: CFS) ────────────────────────


class CFSForcingAcquirer(_AcquisitionBase):
    """SYMFLUENCE acquisition handler that downloads forcing via CFS.

    Selected with ``FORCING_DATASET: CFS``. Configuration keys (flat / YAML):

    ``CFS_PRODUCT`` (required)
        CFS product id ``"provider:product"`` (e.g. ``"aorc:aorc.v1.1"``,
        ``"era5_arco:single_levels"``) or a bare provider slug when the
        provider offers exactly one product (e.g. ``"aorc"``).
    ``CFS_VARIABLES`` (optional)
        Comma-separated canonical variable names (see
        ``cfs.core.vocabulary.CanonicalVar``). Default: all the product offers.
    ``CFS_CONNECTOR_CONFIG`` (optional)
        Provider-specific connector configuration dict
        (e.g. ``{"members": ["gec00"]}`` for GEFS).

    The bounding box and time range come from the standard SYMFLUENCE domain
    config (``BOUNDING_BOX_COORDS``, ``EXPERIMENT_TIME_START/END``). The
    canonical dataset returned by CFS is dask-lazy; it is streamed straight to
    a compressed NetCDF in ``output_dir``.
    """

    def download(self, output_dir: Path) -> Path:
        """Fetch the configured CFS product and write it as one NetCDF file."""
        import cfs

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        cfs.discover()
        providers = cfs.list_providers()

        product = self._get_config_value(lambda: None, default=None, dict_key="CFS_PRODUCT")
        if not product:
            raise ValueError(
                "CFS_PRODUCT is required when FORCING_DATASET is 'CFS'. Set it to a CFS "
                "product id ('provider:product', e.g. 'aorc:aorc.v1.1' or "
                "'era5_arco:single_levels') or a bare provider slug. "
                f"Available CFS providers: {', '.join(providers)}"
            )
        product = str(product)
        slug = product.split(":", 1)[0]
        if slug not in providers:
            raise ValueError(
                f"Unknown CFS product '{product}': no CFS provider '{slug}'. "
                f"Available CFS providers: {', '.join(providers)}"
            )

        variables_cfg = self._get_config_value(lambda: None, default=None, dict_key="CFS_VARIABLES")
        if isinstance(variables_cfg, str):
            variables = [v.strip() for v in variables_cfg.split(",") if v.strip()] or None
        else:
            variables = variables_cfg  # None, or already a list from YAML
        connector_config = self._get_config_value(
            lambda: None, default=None, dict_key="CFS_CONNECTOR_CONFIG"
        )

        out_path = _community_output_path(self, output_dir, _product_tag(product))
        if self._skip_if_exists(out_path):
            return out_path

        bbox = _bbox_tuple(self)
        self.logger.info(
            f"Acquiring CFS product '{product}' for bbox {bbox}, "
            f"{self.start_date} to {self.end_date}"
            + (f", variables: {variables}" if variables else "")
        )

        ds, result = cfs.fetch_sync(
            product,
            bbox=bbox,
            time_range=(self.start_date.to_pydatetime(), self.end_date.to_pydatetime()),
            variables=variables,
            config=connector_config,
        )
        for warning in result.warnings:
            self.logger.warning(f"CFS QC: {warning}")

        # The canonical dataset is dask-lazy; to_netcdf streams it to disk
        # without materializing the full cube in memory.
        ds.to_netcdf(out_path, encoding=_netcdf_encoding(ds))
        ds.close()

        self.logger.info(
            f"CFS acquisition complete: {out_path} "
            f"({result.n_times} timesteps, {result.n_lat}x{result.n_lon} cells)"
        )
        return out_path


# ── Shadow acquisition wrappers (drop-in mode) ───────────────────────


class _ShadowAcquirerBase(_AcquisitionBase):
    """Backend-routing wrapper registered under a native forcing dataset name.

    Subclasses are generated per :class:`ShadowSpec` at :func:`register` time
    (the captured native class only exists then). Constructor signature is the
    native one — ``(config, logger, reporting_manager=None)`` — so SYMFLUENCE's
    ``AcquisitionRegistry.get_handler`` instantiates it unchanged.
    """

    #: Marker so register() never captures a shadow as "the native class".
    _cfs_shadow = True
    _spec: ShadowSpec
    #: The native SYMFLUENCE handler class captured at register() time.
    _native_cls: Any = None

    # -- backend routing -------------------------------------------------

    def _resolve_backend(self) -> str:
        """``<NAME>_BACKEND`` flat key overrides the global ``DATA_ACCESS`` gate."""
        for key in _backend_keys(self._spec):
            value = self._get_config_value(lambda: None, default=None, dict_key=key)
            if value:
                return str(value).strip().lower()
        data_access = self._get_config_value(
            lambda: self.config.domain.data_access, default="MAF", dict_key="DATA_ACCESS"
        )
        return "community" if str(data_access or "MAF").strip().lower() == "community" else "native"

    def download(self, output_dir: Path) -> Path:
        backend = self._resolve_backend()
        if backend != "community":
            return self._native_download(output_dir)
        if self._spec.product is None:
            return self._nex_community_download(output_dir)
        return self._community_download(output_dir)

    # -- native delegation (the bit-identical default) ---------------------

    def _native_download(self, output_dir: Path) -> Path:
        """Run exactly the captured native handler — bit-identical guarantee."""
        if self._native_cls is None:
            raise RuntimeError(
                f"No native SYMFLUENCE acquisition handler was captured for "
                f"'{self._spec.family}' when the CFS plugin registered, so the native "
                f"backend is unavailable. Set DATA_ACCESS: community (or "
                f"{_backend_keys(self._spec)[0]}: community) to acquire via CFS, or "
                "fix the SYMFLUENCE installation so its in-tree handler registers."
            )
        native = self._native_cls(self.config, self.logger, getattr(self, "reporting_manager", None))
        self.logger.debug(f"{self._spec.family}: delegating to native SYMFLUENCE backend")
        return cast(Path, native.download(Path(output_dir)))

    # -- community acquisition --------------------------------------------

    def _community_variables(self) -> list[str] | None:
        """Canonical equivalents of the natively-configured variable list.

        ``None`` (no per-dataset variable key, or key unset) means "all the
        canonical variables the CFS product offers".
        """
        spec = self._spec
        if spec.variables_key is None or spec.native_to_canonical is None:
            return None
        requested = self._get_config_value(lambda: None, default=None, dict_key=spec.variables_key)
        if not requested:
            return None
        if isinstance(requested, str):
            requested = [v.strip() for v in requested.split(",") if v.strip()]
        canonical_names = set(CFS_TO_CFIF_MAPPING) | {"dewpoint_temperature"}
        canonical: list[str] = []
        unmapped: list[str] = []
        for name in requested:
            target = spec.native_to_canonical.get(str(name))
            if target is None and str(name) in canonical_names:
                target = str(name)  # already a canonical-v1 name
            if target is None:
                unmapped.append(str(name))
            elif target not in canonical:
                canonical.append(target)
        if unmapped:
            # Loud, not silent: a model fed a subset of its forcing variables
            # produces garbage downstream.
            self.logger.warning(
                f"{spec.family}: requested {spec.variables_key} variable(s) with no "
                f"canonical-v1 counterpart are NOT included in the community fetch: "
                f"{', '.join(unmapped)}. Set {_backend_keys(spec)[0]}: native if you need them."
            )
        if not canonical:
            raise ValueError(
                f"{spec.family}: none of the requested {spec.variables_key} variables "
                f"({', '.join(map(str, requested))}) map to canonical-v1 names; cannot "
                "fetch via the community backend."
            )
        return canonical

    def _community_download(self, output_dir: Path) -> Path:
        """Fixed-product community fetch (ERA5 / NLDAS / AORC)."""
        import cfs

        product = cast(str, self._spec.product)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = _community_output_path(self, output_dir, _product_tag(product))
        if self._skip_if_exists(out_path):
            return out_path

        variables = self._community_variables()
        bbox = _bbox_tuple(self)
        self.logger.info(
            f"{self._spec.family}: acquiring via CFS community backend "
            f"(product '{product}', bbox {bbox}, {self.start_date} to {self.end_date})"
        )
        ds, result = cfs.fetch_sync(
            product,
            bbox=bbox,
            time_range=(self.start_date.to_pydatetime(), self.end_date.to_pydatetime()),
            variables=variables,
        )
        for warning in result.warnings:
            self.logger.warning(f"CFS QC: {warning}")

        if self._spec.family == "ERA5":
            ds = _add_native_era5_derivations(ds)

        # Keep the canonical-v1 attrs intact: the global `cfs_schema` attr is
        # how the dataset-handler shadows detect community-acquired files.
        ds.to_netcdf(out_path, encoding=_netcdf_encoding(ds))
        ds.close()
        self.logger.info(
            f"{self._spec.family} community acquisition complete: {out_path} "
            f"({result.n_times} timesteps, {result.n_lat}x{result.n_lon} cells)"
        )
        return out_path

    def _nex_community_download(self, output_dir: Path) -> Path:
        """NEX-GDDP community fetch: product + config from the native NEX_* keys.

        Reads the same keys as the native handler (``handlers/nex_gddp.py``):
        ``NEX_MODELS`` (required), ``NEX_SCENARIOS`` (default ['historical']),
        ``NEX_ENSEMBLES`` (default ['r1i1p1f1']), ``NEX_VARIABLES`` (default:
        the native list; canonical-mappable subset is fetched). One canonical
        NetCDF per model x scenario x member combination.
        """
        import cfs
        from cfs.connectors.nex_gddp import SCENARIOS

        models = self._get_config_value(lambda: self.config.forcing.nex.models, dict_key="NEX_MODELS")
        if not models:
            raise ValueError("NEX_MODELS must be set.")
        scenarios = self._get_config_value(
            lambda: self.config.forcing.nex.scenarios, default=["historical"], dict_key="NEX_SCENARIOS"
        )
        members = self._get_config_value(
            lambda: self.config.forcing.nex.ensembles, default=["r1i1p1f1"], dict_key="NEX_ENSEMBLES"
        )
        models = [models] if isinstance(models, str) else list(models)
        scenarios = [scenarios] if isinstance(scenarios, str) else list(scenarios)
        members = [members] if isinstance(members, str) else list(members)

        variables = self._community_variables()
        bbox = _bbox_tuple(self)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        start = self.start_date.to_pydatetime()
        end = self.end_date.to_pydatetime()

        written: list[Path] = []
        for model in models:
            for scenario in scenarios:
                extent = SCENARIOS.get(str(scenario))
                window_start, window_end = start, end
                if extent is not None:
                    window_start = max(window_start, datetime(extent[0], 1, 1))
                    window_end = min(window_end, datetime(extent[1], 12, 31, 23, 59, 59))
                if window_start > window_end:
                    self.logger.info(
                        f"NEX-GDDP: skipping {model}/{scenario}: experiment window has no "
                        f"overlap with the scenario extent {extent}"
                    )
                    continue
                for member in members:
                    tag = _product_tag(f"nex_gddp_{scenario}_{model}_{member}")
                    out_path = _community_output_path(self, output_dir, tag)
                    if self._skip_if_exists(out_path):
                        written.append(out_path)
                        continue
                    self.logger.info(
                        f"NEX-GDDP: acquiring via CFS community backend "
                        f"(nex_gddp:{scenario}, model={model}, member={member})"
                    )
                    ds, result = cfs.fetch_sync(
                        f"nex_gddp:{scenario}",
                        bbox=bbox,
                        time_range=(window_start, window_end),
                        variables=variables,
                        config={"model": str(model), "member": str(member)},
                    )
                    for warning in result.warnings:
                        self.logger.warning(f"CFS QC: {warning}")
                    ds = self._nex_add_synthetic_pressure(ds)
                    ds.to_netcdf(out_path, encoding=_netcdf_encoding(ds))
                    ds.close()
                    written.append(out_path)

        if not written:
            raise RuntimeError(
                "NEX-GDDP-CMIP6 (community backend): no data written — every "
                "model/scenario combination fell outside its scenario extent."
            )
        self.logger.info(f"NEX-GDDP community acquisition complete: {len(written)} file(s) in {output_dir}")
        return output_dir

    def _nex_add_synthetic_pressure(self, ds: xr.Dataset) -> xr.Dataset:
        """Fabricate constant surface pressure, mirroring the native NEX handler.

        NEX-GDDP-CMIP6 publishes no surface pressure, but downstream models
        require it; the native handler synthesizes ``p0 * exp(-z/H)`` from
        ``DOMAIN_MEAN_ELEV_M``. The community file gets the same constant under
        the canonical name so the drop-in pipeline stays complete.
        """
        if "surface_air_pressure" in ds.data_vars:  # pragma: no cover - future NEX pressure
            return ds
        import numpy as np
        import xarray

        elev_cfg = self._get_config_value(lambda: None, default=None, dict_key="DOMAIN_MEAN_ELEV_M")
        if elev_cfg is None:
            self.logger.warning(
                "DOMAIN_MEAN_ELEV_M is not set: the synthetic 'surface_air_pressure' variable "
                "will be a CONSTANT SEA-LEVEL pressure (101325 Pa) across the whole domain. For "
                "an elevation-adjusted estimate, set DOMAIN_MEAN_ELEV_M (domain mean elevation "
                "in metres) in your configuration."
            )
        p_surf = 101325.0 * float(np.exp(-float(elev_cfg or 0.0) / 8400.0))
        template_name = "air_temperature" if "air_temperature" in ds.data_vars else next(iter(ds.data_vars))
        pressure = xarray.full_like(ds[template_name], p_surf, dtype="float32")
        pressure.attrs = {
            "units": "Pa",
            "standard_name": "surface_air_pressure",
            "long_name": "synthetic surface air pressure",
            "note": (
                "Constant p0*exp(-z/H) fabricated by the CFS SYMFLUENCE shadow; "
                "NEX-GDDP-CMIP6 publishes no surface pressure (mirrors the native handler)."
            ),
        }
        ds["surface_air_pressure"] = pressure
        return ds


def _make_shadow_acquirer(spec: ShadowSpec, native_cls: Any) -> type:
    """Build the shadow wrapper class for one dataset family."""
    name = f"CFSShadow{re.sub(r'[^A-Za-z0-9]+', '', spec.family)}Acquirer"
    doc = (
        f"CFS shadow wrapper for the native '{spec.family}' acquisition handler.\n\n"
        f"Backend routing: DATA_ACCESS: community -> CFS "
        f"({spec.product or 'nex_gddp:<scenario>'}); anything else -> the captured "
        f"native class ({getattr(native_cls, '__name__', 'MISSING')}). Per-dataset "
        f"opt-out: {_backend_keys(spec)[0]}: native. Parity: {spec.parity}."
    )
    return type(
        name,
        (_ShadowAcquirerBase,),
        {"_spec": spec, "_native_cls": native_cls, "__doc__": doc, "__module__": __name__},
    )


# ── Dataset (CFIF preprocessing) handlers ────────────────────────────


class CFSDatasetHandler(_DatasetBase):
    """SYMFLUENCE dataset (CFIF preprocessing) handler for CFS forcing.

    CFS already returns canonical-v1 data: CF-aligned names, SI units, and
    fluxes (never accumulations) — exactly CFIF's conventions. So unlike e.g.
    the ERA5 handler there are no unit conversions or de-accumulations here:
    :meth:`process_dataset` is a (mostly identity) rename plus CFIF attribute
    application, after rejecting projected-grid products (v1 limitation).
    """

    def get_variable_mapping(self) -> dict[str, str]:
        """CFS canonical-v1 names -> CFIF names (identity where they coincide)."""
        return dict(CFS_TO_CFIF_MAPPING)

    def process_dataset(self, ds: xr.Dataset) -> xr.Dataset:
        """Rename canonical variables to CFIF and apply standard attributes.

        Data arrive in canonical SI units identical to CFIF's (K, kg m-2 s-1,
        W m-2, Pa, m s-1), so no unit conversion is performed.
        """
        return _canonical_to_cfif(self, ds)

    def get_coordinate_names(self) -> tuple[str, str]:
        """CFS regular-grid canonical datasets use 1-D latitude/longitude."""
        return ("latitude", "longitude")

    def needs_merging(self) -> bool:
        """Standardize raw files into the merged layout (rename + attributes)."""
        return True

    def merge_forcings(
        self, raw_forcing_path: Path, merged_forcing_path: Path, start_year: int, end_year: int
    ) -> None:
        """Copy raw files to the merged layout via :meth:`process_dataset`.

        Delegates to the ERA5 handler's generic implementation (glob raw
        ``*.nc``, run ``self.process_dataset``, write atomically), which is
        grid- and dataset-agnostic for regular lat/lon products.
        """
        from symfluence.data.preprocessing.dataset_handlers.era5_utils import ERA5Handler

        ERA5Handler.merge_forcings(self, raw_forcing_path, merged_forcing_path, start_year, end_year)

    def create_shapefile(
        self, shapefile_path: Path, merged_forcing_path: Path, dem_path: Path, elevation_calculator
    ) -> Path:
        """Build the forcing-grid shapefile for EASYMORE remapping weights.

        Delegates to the ERA5 handler's regular lat/lon grid implementation,
        which reads 1-D ``latitude``/``longitude`` — exactly the canonical-v1
        regular-grid layout this handler supports.
        """
        from symfluence.data.preprocessing.dataset_handlers.era5_utils import ERA5Handler

        return cast(
            Path,
            ERA5Handler.create_shapefile(
                self, shapefile_path, merged_forcing_path, dem_path, elevation_calculator
            ),
        )


class _ShadowDatasetHandlerBase(CFSDatasetHandler):
    """Self-detecting dataset-handler shadow registered under a native key.

    Every method that touches raw files first detects canonical-v1 (the
    ``cfs_schema`` global attribute on one raw file — cheap, and the verdict
    is cached): canonical files take the CFS canonical→CFIF path, anything
    else delegates to the captured native dataset handler instance.
    ``process_dataset`` detects per dataset, so a domain can mix cached
    native raw files with community-acquired canonical ones.

    Where SYMFLUENCE has no native handler under the key (NLDAS family),
    ``_native_cls`` is ``None`` and non-canonical input raises a clear error:
    those keys serve community-acquired files only.
    """

    _cfs_shadow = True
    _spec: ShadowSpec
    _native_cls: Any = None

    def __init__(self, config: Any, logger: Any, project_dir: Path, **kwargs: Any) -> None:
        super().__init__(config, logger, project_dir, **kwargs)
        self._native = (
            self._native_cls(config, logger, project_dir, **kwargs)
            if self._native_cls is not None
            else None
        )
        self._canonical_verdict: bool | None = None

    # -- detection ---------------------------------------------------------

    @staticmethod
    def _is_canonical(ds: Any) -> bool:
        """A canonical-v1 dataset carries the ``cfs_schema`` global attribute."""
        return str(getattr(ds, "attrs", {}).get("cfs_schema", "")).startswith("canonical-")

    def _probe_dir(self, path: Any) -> bool | None:
        """Open one ``*.nc`` file under ``path`` and check ``cfs_schema``; cache the verdict."""
        import xarray

        if not path:
            return None
        path = Path(path)
        if not path.is_dir():
            return None
        files = sorted(path.glob("*.nc"))
        if not files:
            return None
        try:
            with xarray.open_dataset(files[0]) as ds:
                verdict = self._is_canonical(ds)
        except Exception:  # noqa: BLE001 - unreadable file: leave undetermined
            return None
        self._canonical_verdict = verdict
        return verdict

    def _use_canonical(self, *candidates: Any) -> bool:
        """Resolve (and cache) the canonical-vs-native verdict.

        Probes the given candidate directories first, then the project's raw
        forcing directories. When nothing is determinable yet, prefer the
        native handler when one exists (exact native behavior), else the
        canonical path (the only thing these keys can serve).
        """
        if self._canonical_verdict is not None:
            return self._canonical_verdict
        for candidate in candidates:
            verdict = self._probe_dir(candidate)
            if verdict is not None:
                return verdict
        for default_dir in (
            Path(self.project_dir) / "data" / "forcing" / "raw_data",
            Path(self.project_dir) / "forcing" / "raw_data",
        ):
            verdict = self._probe_dir(default_dir)
            if verdict is not None:
                return verdict
        return self._native is None

    def _require_native(self, context: str) -> Any:
        if self._native is None:
            raise NotImplementedError(
                f"{context}, but SYMFLUENCE has no native dataset handler under the "
                f"'{self._spec.dataset_keys[0]}' preprocessing key — the CFS plugin provides it "
                "for community-acquired canonical-v1 files only. Re-acquire the forcing with "
                "DATA_ACCESS: community, or use a forcing dataset with a native handler."
            )
        return self._native

    # -- BaseDatasetHandler contract ----------------------------------------

    def get_variable_mapping(self) -> dict[str, str]:
        if self._use_canonical():
            return dict(CFS_TO_CFIF_MAPPING)
        native = self._require_native("The raw forcing files are not canonical-v1")
        return cast("dict[str, str]", native.get_variable_mapping())

    def process_dataset(self, ds: xr.Dataset) -> xr.Dataset:
        # Per-dataset detection (not the cached dir verdict): mixed raw
        # directories route each file to the right pipeline.
        if self._is_canonical(ds):
            return _canonical_to_cfif(self, ds)
        native = self._require_native("process_dataset() received a non-canonical dataset")
        return cast("xr.Dataset", native.process_dataset(ds))

    def get_coordinate_names(self) -> tuple[str, str]:
        if self._use_canonical():
            return ("latitude", "longitude")
        native = self._require_native("The raw forcing files are not canonical-v1")
        return cast("tuple[str, str]", native.get_coordinate_names())

    def needs_merging(self) -> bool:
        if self._use_canonical():
            return True
        native = self._require_native("The raw forcing files are not canonical-v1")
        return cast(bool, native.needs_merging())

    def merge_forcings(
        self, raw_forcing_path: Path, merged_forcing_path: Path, start_year: int, end_year: int
    ) -> None:
        if self._use_canonical(raw_forcing_path):
            # The generic canonical merge calls self.process_dataset per file,
            # which self-detects — mixed directories are handled per file.
            CFSDatasetHandler.merge_forcings(
                self, raw_forcing_path, merged_forcing_path, start_year, end_year
            )
            return
        native = self._require_native("The raw forcing files are not canonical-v1")
        native.merge_forcings(raw_forcing_path, merged_forcing_path, start_year, end_year)

    def create_shapefile(
        self, shapefile_path: Path, merged_forcing_path: Path, dem_path: Path, elevation_calculator
    ) -> Path:
        if self._use_canonical(merged_forcing_path):
            return CFSDatasetHandler.create_shapefile(
                self, shapefile_path, merged_forcing_path, dem_path, elevation_calculator
            )
        native = self._require_native("The merged forcing files are not canonical-v1")
        return cast(
            Path,
            native.create_shapefile(shapefile_path, merged_forcing_path, dem_path, elevation_calculator),
        )


def _make_shadow_dataset_handler(spec: ShadowSpec, native_cls: Any) -> type:
    """Build the self-detecting dataset-handler shadow class for one family."""
    name = f"CFSShadow{re.sub(r'[^A-Za-z0-9]+', '', spec.family)}DatasetHandler"
    if native_cls is None:
        doc = (
            f"CFS canonical dataset handler for the '{spec.dataset_keys[0]}' key. SYMFLUENCE has "
            "no native handler here; community-acquired canonical-v1 files only."
        )
    else:
        doc = (
            f"Self-detecting shadow of {native_cls.__name__} for the '{spec.dataset_keys[0]}' key: "
            "canonical-v1 raw files take the CFS canonical->CFIF path, native-format files "
            "delegate to the captured native handler."
        )
    return type(
        name,
        (_ShadowDatasetHandlerBase,),
        {"_spec": spec, "_native_cls": native_cls, "__doc__": doc, "__module__": __name__},
    )


# ── Registration ─────────────────────────────────────────────────────


def _install_shadows(registries: Any) -> None:
    """Capture native classes and register the shadow wrappers (idempotent).

    Runs after SYMFLUENCE's in-tree handlers registered (plugin discovery is
    last in the bootstrap), so ``Registry.get`` returns the genuine native
    class. On re-registration (entry point + self-register tail) the existing
    shadow is detected via ``_cfs_shadow`` and left in place, so the captured
    native class is never itself a wrapper.
    """
    acquisition = registries.acquisition_handlers
    datasets = registries.dataset_handlers

    for spec in SHADOW_SPECS:
        existing = [acquisition.get(alias) for alias in spec.aliases]
        if not any(getattr(cls, "_cfs_shadow", False) for cls in existing if cls is not None):
            native_cls = next((cls for cls in existing if cls is not None), None)
            wrapper = _make_shadow_acquirer(spec, native_cls)
            for alias in spec.aliases:
                acquisition.add(alias, wrapper)

        existing_ds = [datasets.get(key) for key in spec.dataset_keys]
        if not any(getattr(cls, "_cfs_shadow", False) for cls in existing_ds if cls is not None):
            native_ds_cls = next((cls for cls in existing_ds if cls is not None), None)
            handler_cls = _make_shadow_dataset_handler(spec, native_ds_cls)
            for key in spec.dataset_keys:
                datasets.add(key, handler_cls)


def register() -> None:
    """SYMFLUENCE plugin hook (``symfluence.plugins`` entry point, zero-arg).

    Called by SYMFLUENCE's plugin discovery on ``import symfluence`` — after
    every in-tree handler registered, so the shadow installation can capture
    the genuine native classes. Raises ``ImportError`` when SYMFLUENCE is
    absent — discovery logs and skips a failing plugin, so this is safe by
    design. Re-registration is idempotent: the parallel-name handlers are
    plain overwrites, and already-installed shadows are detected and kept
    (their captured native class is never replaced by a wrapper).
    """
    from symfluence.core.registries import R

    R.acquisition_handlers.add("CFS", CFSForcingAcquirer)
    R.dataset_handlers.add("cfs", CFSDatasetHandler)
    _install_shadows(R)


# Self-register when SYMFLUENCE is importable. This complements the entry
# point: if THIS module is imported before symfluence, the defensive import
# above triggers symfluence's bootstrap mid-module, and its plugin discovery
# then sees a partially-initialized module (no ``register`` yet) and skips the
# cfs entry point. Registering here, at the end of the module body, makes the
# handlers available regardless of import order; register() is idempotent so
# the entry-point path stays harmless.
if HAVE_SYMFLUENCE:  # pragma: no cover - exercised only with SYMFLUENCE present
    import contextlib

    # Never let registration break ``import cfs.integrations.symfluence``.
    with contextlib.suppress(Exception):
        register()
