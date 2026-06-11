# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""SYMFLUENCE integration — CFS as a forcing acquisition + preprocessing plugin.

This module makes every CFS product available inside SYMFLUENCE as a forcing
dataset: set ``FORCING_DATASET: CFS`` and ``CFS_PRODUCT: <provider:product>``
in a SYMFLUENCE config and the framework downloads via :func:`cfs.fetch_sync`,
then renames the canonical-v1 variables to SYMFLUENCE's CFIF vocabulary before
its own EASYMORE HRU remapping.

It is wired up through SYMFLUENCE's ``symfluence.plugins`` entry-point group
(see ``pyproject.toml``): ``import symfluence`` discovers the entry point and
calls :func:`register`, which adds

* :class:`CFSForcingAcquirer` to ``R.acquisition_handlers`` under ``'CFS'``, and
* :class:`CFSDatasetHandler` to ``R.dataset_handlers`` under ``'cfs'``.

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
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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

        if not self.bbox:
            raise ValueError(
                "No bounding box available for the CFS acquisition handler. "
                "Set BOUNDING_BOX_COORDS ('north/west/south/east') in your configuration."
            )

        product_tag = re.sub(r"[^A-Za-z0-9]+", "_", product).strip("_").lower()
        start_str = self.start_date.strftime("%Y%m%d")
        end_str = self.end_date.strftime("%Y%m%d")
        out_path = output_dir / f"domain_{self.domain_name}_cfs_{product_tag}_{start_str}_{end_str}.nc"
        if self._skip_if_exists(out_path):
            return out_path

        bbox = (
            float(self.bbox["lon_min"]),
            float(self.bbox["lat_min"]),
            float(self.bbox["lon_max"]),
            float(self.bbox["lat_max"]),
        )
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
        _require_regular_grid(ds)

        mapping = self.get_variable_mapping()
        renames = {
            old: new for old, new in mapping.items() if old in ds.variables and new != old
        }
        if renames:  # pragma: no cover - identity mapping today; future-proofing
            ds = ds.rename(renames)

        return cast("xr.Dataset", self.apply_standard_attributes(ds))

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


def register() -> None:
    """SYMFLUENCE plugin hook (``symfluence.plugins`` entry point, zero-arg).

    Called by SYMFLUENCE's plugin discovery on ``import symfluence``. Raises
    ``ImportError`` when SYMFLUENCE is absent — discovery logs and skips a
    failing plugin, so this is safe by design. Re-registration is idempotent:
    ``Registry.add`` overwrites an existing key without raising.
    """
    from symfluence.core.registries import R

    R.acquisition_handlers.add("CFS", CFSForcingAcquirer)
    R.dataset_handlers.add("cfs", CFSDatasetHandler)


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
