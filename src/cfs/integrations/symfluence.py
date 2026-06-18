# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""SYMFLUENCE integration — CFS as a formal AcquisitionBackend.

This module wires CFS into SYMFLUENCE through the ``symfluence.plugins``
entry-point group (see ``pyproject.toml``): ``import symfluence`` discovers the
entry point and calls :func:`register`, which adds exactly two things to the
SYMFLUENCE registries:

1. :class:`CommunityForcingBackend` under ``R.acquisition_backends['community']``
   — an implementation of SYMFLUENCE's versioned ``AcquisitionBackend``
   protocol (``symfluence.data.backends.contract``). The backend *declares*
   its parity-validated datasets via ``capabilities()`` and serves
   ``acquire()`` requests through :func:`cfs.fetch_sync`, writing canonical-v1
   NetCDF plus the sidecar ``acquisition_manifest.json`` that drives all
   downstream schema dispatch. Selection is framework-side
   (``DATA_ACCESS: community`` / per-dataset ``<NAME>_BACKEND`` keys); this
   module no longer overwrites registry entries, captures native classes, or
   self-detects file formats. CFS-internal failures are mapped onto the
   protocol error taxonomy.

2. :class:`CanonicalV1Handler` under ``R.dataset_handlers['canonical-v1']`` —
   ONE schema-keyed preprocessing handler for every canonical-v1 raw file,
   regardless of dataset. SYMFLUENCE resolves it from the acquisition
   manifest's declared schema (never by sniffing files). It supports both
   grid classes of the canonical-v1 spec: regular 1-D latitude/longitude
   grids and projected grids (rotated-pole / LCC) carrying native ``rlat``/
   ``rlon`` or ``y``/``x`` dims with 2-D latitude/longitude coordinates.

The module is intentionally decoupled (same pattern as before):

* SYMFLUENCE types are resolved defensively at import time; when SYMFLUENCE is
  absent the bases degrade so ``import cfs.integrations.symfluence`` never
  fails.
* :func:`register` *does* import SYMFLUENCE — when it is absent the resulting
  ``ImportError`` is exactly what SYMFLUENCE's plugin discovery expects and
  silently skips.
* CFS itself is imported lazily inside methods (house style for heavy deps).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

if TYPE_CHECKING:
    import xarray as xr

# True only once this module has finished importing. The symfluence import
# below triggers SYMFLUENCE's bootstrap; if THIS module was imported before
# symfluence, that bootstrap re-enters to load the cfs plugin entry point while
# this module is still partially initialized. ``register`` is defined here,
# ahead of that import, and no-ops until the module is ready — so the re-entrant
# call is quiet (no "failed to load … skipping" warning) and the module-bottom
# self-register performs the real registration.
_MODULE_READY = False


def register() -> None:
    """SYMFLUENCE plugin hook (``symfluence.plugins`` entry point, zero-arg).

    Defers while this module is still importing (re-entrant bootstrap); the real
    work is in :func:`_register_impl`.
    """
    if not _MODULE_READY:  # pragma: no cover - re-entrant bootstrap during import
        return
    _register_impl()


# Resolve the SYMFLUENCE pieces defensively so importing this module never
# hard-fails when SYMFLUENCE is not installed.
try:  # pragma: no cover - exercised only with SYMFLUENCE present
    from symfluence.data.backends import contract as _contract
    from symfluence.data.backends import errors as _errors
    from symfluence.data.preprocessing.dataset_handlers.base_dataset import (
        BaseDatasetHandler as _DatasetBase,
    )

    HAVE_SYMFLUENCE = True
except Exception:  # noqa: BLE001 - any import failure means "not available"
    _contract = None  # type: ignore[assignment]
    _errors = None  # type: ignore[assignment]
    _DatasetBase = object  # type: ignore[assignment, misc]
    HAVE_SYMFLUENCE = False


#: Semver of the SYMFLUENCE AcquisitionBackend protocol this backend targets.
#: Deliberately hardcoded (not read from the installed framework) so that a
#: contract bump on the SYMFLUENCE side is *detected* as skew by the selection
#: layer instead of silently claimed compatible.
#:
#: 0.2.0 (2026-06-12): the bump added the *observation* flavour
#: (ObservationBackend/ObservationCapability/ObservationRequest) and left the
#: AcquisitionBackend surface untouched — verified against the contract diff
#: before re-targeting.
TARGET_INTERFACE_VERSION = "0.4.0"


#: Source-data license posture per dataset family (acquisition contract 0.4.0):
#: (redistribution, data_license, attribution). A community backend re-serves
#: regridded data from its own store, which is redistribution — so the framework
#: gate refuses any family declared ``restricted`` and propagates the attribution
#: string. Verified against primary licence sources (2026-06; note Copernicus
#: moved to CC-BY-4.0 on 2025-07-02, clearing ERA5/CARRA/CERRA/WFDE5). Families
#: not listed default to ``("unknown", "", "")``. Known-restricted families
#: (E-OBS non-commercial+registration, MSWEP CC-BY-NC, NA-CORDEX CC-BY-NC-SA) are
#: declared restricted here and deliberately NOT exposed in DATASET_SPECS.
_FORCING_POSTURE: dict[str, tuple[str, str, str]] = {
    "ERA5": ("attribution", "CC-BY-4.0", "Copernicus Climate Change Service (C3S)"),
    "CARRA": ("attribution", "CC-BY-4.0", "Copernicus Climate Change Service (C3S)"),
    "CERRA": ("attribution", "CC-BY-4.0", "Copernicus Climate Change Service (C3S)"),
    "NLDAS": ("open", "CC0-1.0", "NASA Land Data Assimilation Systems"),
    "NEX-GDDP": ("open", "CC0-1.0", "NASA NEX-GDDP-CMIP6 (NCCS)"),
    "CONUS404": ("open", "CC0-1.0", "U.S. Geological Survey (CONUS404)"),
    "DAYMET": ("open", "public-domain", "ORNL DAAC (Daymet V4)"),
    "AORC": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA Office of Water Prediction (AORC)"),
    "NWM3": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA National Water Model"),
    "HRRR": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA High-Resolution Rapid Refresh"),
    # The "CFS" entry is the generic any-product passthrough, not a specific
    # verified dataset — keep it UNKNOWN so it stays opt-in-gated (the framework
    # refuses it unless ALLOW_UNGATED_BACKENDS), rather than admitted by posture.
    "CFS": ("unknown", "", ""),
    "RDRS": ("attribution", "ECCC-Data-Servers-End-use-Licence-2.1", "Environment and Climate Change Canada"),
    "CASR": ("attribution", "ECCC-Data-Servers-End-use-Licence-2.1", "Environment and Climate Change Canada"),
    # Posture-only expansion tier (verified, open/attribution):
    "GFS": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA Global Forecast System"),
    "CFSV2": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA NCEP Climate Forecast System v2 / CDAS"),
    "NARR": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA North American Regional Reanalysis"),
    "NAM": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA North American Mesoscale model"),
    "RAP": ("attribution", "US-Public-Domain-NOAA-NODD", "NOAA Rapid Refresh"),
    "NCLIMGRID": ("open", "US-Public-Domain-NOAA-NCEI", "NOAA NCEI nClimGrid-daily"),
    "PERSIANN-CDR": ("open", "US-Public-Domain-NOAA-NCEI", "NOAA NCEI / UC Irvine CHRS PERSIANN-CDR"),
    "GRIDMET": ("open", "CC0-1.0", "University of Idaho gridMET (Abatzoglou)"),
    "CHIRPS": ("open", "CC-BY-4.0", "UCSB Climate Hazards Center (CHIRPS)"),
    "LIVNEH": ("open", "US-Public-Domain", "NOAA PSL / Livneh et al. (2015)"),
    "TERRACLIMATE": ("open", "CC0-1.0", "University of Idaho TerraClimate (Abatzoglou et al.)"),
    "MRMS": ("open", "US-Public-Domain", "NOAA MRMS QPE"),
    "W5E5": ("open", "CC0-1.0", "ISIMIP / W5E5 v2.0 (Lange et al.)"),
    "GLDAS": ("attribution", "CC0-1.0", "NASA Global Land Data Assimilation System"),
    "FLDAS": ("attribution", "CC0-1.0", "NASA Famine Early Warning LDAS"),
    "GPM": ("attribution", "CC0-1.0", "NASA GPM IMERG"),
    "MERRA2": ("attribution", "CC0-1.0", "NASA MERRA-2"),
    "ECMWF": ("attribution", "CC-BY-4.0", "ECMWF (IFS open data)"),
    # AgERA5: CC-BY-4.0 since the 2025-07-02 Copernicus migration. Live-verified
    # via CDS, but auth-gated + partial coverage, so kept out of DATASET_SPECS
    # (like WFDE5) — the ungraded posture-only tier excludes auth-gated families.
    "AGERA5": ("attribution", "CC-BY-4.0", "Copernicus Climate Change Service (C3S) AgERA5"),
    # DWD ICON-EU open data (near-real-time only, no by-date archive); declared
    # posture, not in DATASET_SPECS (like the ECCC forecast suite).
    "ICON": ("attribution", "CC-BY-4.0", "Deutscher Wetterdienst (DWD)"),
    # ECCC/MSC operational forecast suite (same end-use licence as RDRS/CASR).
    # Near-real-time only (MSC Datamart keeps no by-date archive), so these carry
    # a declared posture but are deliberately NOT in DATASET_SPECS — the community
    # backend serves historical forcing windows the latest-run-only feed cannot
    # satisfy (mirrors the gefs/nwm_operational deferral).
    "HRDPS": ("attribution", "ECCC-Data-Servers-End-use-Licence-2.1", "Environment and Climate Change Canada"),
    "RDPS": ("attribution", "ECCC-Data-Servers-End-use-Licence-2.1", "Environment and Climate Change Canada"),
    "GEPS": ("attribution", "ECCC-Data-Servers-End-use-Licence-2.1", "Environment and Climate Change Canada"),
}


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

#: The eight standard CFIF grid-forcing variables.
_STANDARD_8 = frozenset({
    "air_temperature",
    "precipitation_flux",
    "specific_humidity",
    "surface_air_pressure",
    "surface_downwelling_shortwave_flux",
    "surface_downwelling_longwave_flux",
    "eastward_wind",
    "northward_wind",
})

#: NLDAS-2 v2.0 NetCDF (ALMA) names -> canonical-v1, mirroring the CFS
#: ``nldas`` connector's source mapping (connectors/nldas.py). Kept so the
#: legacy ``NLDAS_VARIABLES`` config key (native names) keeps working.
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

#: HRRR GRIB short names -> canonical-v1 (legacy ``HRRR_VARS`` config key).
_HRRR_TO_CANONICAL: dict[str, str] = {
    "TMP": "air_temperature",
    "SPFH": "specific_humidity",
    "PRES": "surface_air_pressure",
    "UGRD": "eastward_wind",
    "VGRD": "northward_wind",
    "DSWRF": "surface_downwelling_shortwave_flux",
    "DLWRF": "surface_downwelling_longwave_flux",
}

#: NWM v3.0 retrospective forcing names -> canonical-v1 (``NWM3_VARIABLES``).
_NWM3_TO_CANONICAL: dict[str, str] = {
    "T2D": "air_temperature",
    "Q2D": "specific_humidity",
    "PSFC": "surface_air_pressure",
    "U2D": "eastward_wind",
    "V2D": "northward_wind",
    "SWDOWN": "surface_downwelling_shortwave_flux",
    "LWDOWN": "surface_downwelling_longwave_flux",
    "RAINRATE": "precipitation_flux",
}

#: Daymet raw names -> canonical-v1 (``DAYMET_VARIABLES``). tmax/tmin/dayl all
#: collapse onto the canonical field their derivation feeds; ``swe`` has no
#: canonical counterpart and is dropped loudly.
_DAYMET_TO_CANONICAL: dict[str, str] = {
    "tmax": "air_temperature",
    "tmin": "air_temperature",
    "prcp": "precipitation_flux",
    "srad": "surface_downwelling_shortwave_flux",
    "dayl": "surface_downwelling_shortwave_flux",
    "vp": "dewpoint_temperature",
}


# ── The capability map ───────────────────────────────────────────────


class DatasetSpec(NamedTuple):
    """One dataset the community backend can serve.

    Attributes:
        family: Human-readable dataset family name (logs/errors/dispatch).
        dataset_ids: Every framework name this dataset is requestable under —
            each gets its own :class:`DatasetCapability` entry.
        product: Fixed CFS product id, or ``None`` for NEX-GDDP (built per
            scenario from the native config keys) and for the parallel-name
            ``CFS`` entry (product comes from ``options['product']`` /
            ``CFS_PRODUCT``).
        grid: ``"regular_latlon"`` or ``"projected"`` (contract GridClass value).
        variables: CFIF names servable (capability declaration).
        fetchable: Canonical names the CFS product actually offers; requested
            variables outside this set are satisfied by derivation
            (``wind_speed``, ERA5 ``specific_humidity``) or dropped loudly.
        auth: Auth-provider ids required (contract vocabulary).
        temporal: Declared coverage ``[start, end)`` or ``None``.
        spatial: Spatial domain ``(west, south, east, north)`` in degrees, or
            ``None`` for (effectively) global products. Regional datasets
            (CARRA is Arctic-only, CERRA Europe-only, HRRR CONUS-only, Daymet
            North-America-only) refuse out-of-domain bboxes at ``acquire()``
            time with a clear error: the limit is a property of the *dataset*,
            not of this backend, so there is nothing to fall back to. (The
            AcquisitionBackend contract has no spatial capability field yet,
            so selection cannot decline on bbox; this is the minimal honest
            check until it does.)
        parity: Parity grade vs the native pipeline (``None`` = ungated; the
            framework refuses ungated datasets unless ALLOW_UNGATED_BACKENDS).
        variables_key: Legacy flat config key holding a native variable list.
        native_to_canonical: Native variable name -> canonical-v1 map for
            translating ``variables_key`` entries.
        notes: Capability notes (shown to users by tooling).
    """

    family: str
    dataset_ids: tuple[str, ...]
    product: str | None
    grid: str
    variables: frozenset[str]
    fetchable: frozenset[str]
    auth: frozenset[str]
    temporal: tuple[str, str] | None
    spatial: tuple[float, float, float, float] | None
    parity: str | None
    variables_key: str | None
    native_to_canonical: dict[str, str] | None
    notes: str


#: Parity-gated dataset map: ONLY datasets whose native-vs-community output
#: was live-validated are graded (2026-06-11/12 experiments; see
#: docs/symfluence.md). MSWEP and EM-EARTH remain deliberately ABSENT until
#: live parity validation is possible (blocked: no rclone Drive remote / the
#: EM-Earth S3 bucket denies anonymous GET) — their native handlers keep
#: running untouched under every DATA_ACCESS value. The parallel-name ``CFS``
#: entry is intentionally ungraded (``parity=None``) and exercises the
#: framework's ungated-backend policy.
DATASET_SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        family="ERA5",
        dataset_ids=("ERA5",),
        product="era5_arco:single_levels",
        grid="regular_latlon",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=frozenset({
            "air_temperature", "dewpoint_temperature", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux",
        }),
        auth=frozenset(),
        temporal=None,
        spatial=None,
        parity="value-identical:2ulp",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "ARCO ERA5 (anonymous GCS Zarr). The 3 accumulation->flux variables "
            "differ <= 2 float32 ulps from native (operation order only); "
            "wind_speed / specific_humidity are derived with the native float32 "
            "op order (bitwise equal). The CDS pathway (ERA5_CDS) stays native."
        ),
    ),
    DatasetSpec(
        family="NLDAS",
        dataset_ids=("NLDAS", "NLDAS2", "NLDAS-2"),
        product="nldas:fora0125_h",
        grid="regular_latlon",
        variables=_STANDARD_8,
        fetchable=_STANDARD_8,
        auth=frozenset({"earthdata"}),
        temporal=None,
        spatial=None,
        parity="value-identical:1ulp",
        variables_key="NLDAS_VARIABLES",
        native_to_canonical=_NLDAS_TO_CANONICAL,
        notes="NLDAS-2 v2.0 hourly forcing; 7/8 variables bitwise, precip <= 1 float32 ulp.",
    ),
    DatasetSpec(
        family="AORC",
        dataset_ids=("AORC",),
        product="aorc:conus_1km",
        grid="regular_latlon",
        variables=_STANDARD_8,
        fetchable=_STANDARD_8,
        auth=frozenset(),
        temporal=("2002-02-01", "2100-01-01"),
        spatial=None,
        parity="bit-identical",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "AORC 1 km CONUS Zarr (anonymous S3). Pre-2002 windows are DECLINED "
            "(coverage start 2002-02): SYMFLUENCE's native NWM-projected fallback "
            "serves those. Upper coverage bound is nominal (archive grows)."
        ),
    ),
    DatasetSpec(
        family="NEX-GDDP",
        dataset_ids=("NEX-GDDP-CMIP6", "NEX-GDDP"),
        product=None,  # nex_gddp:<scenario>, built from NEX_* config keys
        grid="regular_latlon",
        variables=frozenset({
            "air_temperature", "precipitation_flux", "specific_humidity",
            "wind_speed", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "surface_air_pressure",
        }),
        fetchable=frozenset({
            "air_temperature", "precipitation_flux", "specific_humidity",
            "wind_speed", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux",
        }),
        auth=frozenset(),
        temporal=None,
        spatial=None,
        parity="bit-identical",
        variables_key="NEX_VARIABLES",
        native_to_canonical=_NEX_TO_CANONICAL,
        notes=(
            "Daily downscaled CMIP6 (same physical files as native: NCCS THREDDS "
            "vs S3 mirror). Driven by NEX_MODELS/NEX_SCENARIOS/NEX_ENSEMBLES; "
            "surface pressure is synthesized exactly like the native handler."
        ),
    ),
    DatasetSpec(
        family="RDRS",
        dataset_ids=("RDRS", "RDRS_v3.1"),
        product="rdrs:casr_v32",
        grid="projected",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=_STANDARD_8 | {"dewpoint_temperature"},
        auth=frozenset(),
        temporal=("1980-01-01", "2025-01-01"),
        spatial=None,
        parity="bit-identical",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "CaSR v3.2 rotated-pole (~10 km hourly) via PAVICS OPeNDAP — the same "
            "store the native handler reads (exp10: all 9 variables + rlat/rlon/"
            "2-D lat/lon + time bitwise identical). wind_speed is derived as "
            "hypot(u, v) during preprocessing; it deviates <= 9e-4 m/s from "
            "CaSR's own sfcWind diagnostic (physically negligible, documented)."
        ),
    ),
    DatasetSpec(
        family="CASR",
        dataset_ids=("CASR",),
        product="rdrs:casr_v32",
        grid="projected",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=_STANDARD_8 | {"dewpoint_temperature"},
        auth=frozenset(),
        temporal=("1980-01-01", "2025-01-01"),
        spatial=None,
        parity="bit-identical",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "Alias of the RDRS capability: SYMFLUENCE's CASR is the same ECCC "
            "CaSR product family. Natively CASR is MAF/datatool-only (HPC-"
            "prestaged CaSR v3.1 extracts, RPN names like CaSR_v3.1_P_TT_1.5m); "
            "the only public cloud upstream is the PAVICS CaSR v3.2 store — the "
            "very store this product reads, verified bitwise against the native "
            "RDRSAcquirer (exp10). Note the served version is v3.2 (the v3.1 "
            "HPC staging stays native-only by definition)."
        ),
    ),
    DatasetSpec(
        family="CONUS404",
        dataset_ids=("CONUS404",),
        product="conus404:hourly",
        grid="projected",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=_STANDARD_8 | {"dewpoint_temperature"},
        auth=frozenset(),
        temporal=("1979-10-01", "2022-10-01"),
        spatial=None,
        parity="value-identical:1ulp",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "USGS/NCAR CONUS404 4 km WRF hourly (HyTEST OSN Zarr — the same "
            "store the native intake-catalog route reads). exp13: T/q/p/u/v and "
            "derived wind_speed bitwise; precip + radiation <= 1 float32 ulp "
            "(op order, /3600 vs *(1/3600)). The first radiation step differs "
            "by design: the native pipeline back-fills it from step 2, the "
            "community pipeline de-accumulates it against a real pre-window "
            "hour (more correct; documented)."
        ),
    ),
    DatasetSpec(
        family="NWM3",
        dataset_ids=("NWM3_RETROSPECTIVE",),
        product="aorc_nwm:conus_1km",
        grid="projected",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=_STANDARD_8,
        auth=frozenset(),
        temporal=("1979-02-01", "2023-02-01"),
        spatial=None,
        parity="bit-identical",
        variables_key="NWM3_VARIABLES",
        native_to_canonical=_NWM3_TO_CANONICAL,
        notes=(
            "NWM v3.0 retrospective forcing (AORC v1.1 on the NWM 1 km LCC "
            "grid; noaa-nwm-retrospective-3-0-pds Zarr stores — the same ones "
            "the native handler opens). exp15: all 8 variables + 2-D lat/lon + "
            "time bitwise. Convention note: canonical precipitation is a flux "
            "(kg m-2 s-1); the native file ships the identical values *3600 "
            "(hourly accumulation) — value-equivalent. Serves only the "
            "'forcing' output type (chrtout/ldasout/... stay native)."
        ),
    ),
    DatasetSpec(
        family="CARRA",
        dataset_ids=("CARRA",),
        product="carra:single_levels",
        grid="regular_latlon",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=_STANDARD_8,
        auth=frozenset({"cds"}),
        temporal=("1990-09-01", "2100-01-01"),
        spatial=(-180.0, 55.0, 180.0, 90.0),
        parity="value-identical:grib-repack",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "Copernicus Arctic Regional Reanalysis via CDS, 3-hourly, "
            "CDS-interpolated to a regular 0.025-deg grid on both sides (the "
            "native handler requests the same `grid` interpolation). exp11: "
            "time bitwise; every field differs only by the CDS/MARS per-"
            "request GRIB re-packing (the native handler pads the area "
            "+-0.1 deg, so the 16-bit packing anchors differ; verified "
            "quantum combs; T <= 1.9e-4 K, p <= 0.18 Pa, fluxes <= 1.6e-4 "
            "rel) plus the documented specific-humidity epsilon derivation "
            "(<= 4.7e-5 rel). Arctic-only (>= 55N); the CDS domain split "
            "(west_domain default / east_domain) follows the native "
            "CARRA_DOMAIN key. Upper coverage bound is nominal (rolling "
            "archive, ~2-3 month latency)."
        ),
    ),
    DatasetSpec(
        family="CERRA",
        dataset_ids=("CERRA",),
        product="cerra:single_levels",
        grid="regular_latlon",
        variables=(_STANDARD_8 - {"eastward_wind", "northward_wind"}) | {"wind_speed"},
        fetchable=(_STANDARD_8 - {"eastward_wind", "northward_wind"}) | {"wind_speed"},
        auth=frozenset({"cds"}),
        temporal=("1984-09-01", "2021-07-01"),
        spatial=(-58.0, 20.0, 74.0, 74.0),
        parity="value-identical:grib-repack",
        variables_key=None,
        native_to_canonical=None,
        notes=(
            "Copernicus European Regional Reanalysis via CDS, 3-hourly, "
            "CDS-interpolated to a regular 0.05-deg grid on both sides. "
            "CERRA publishes a combined 10m wind speed (no u/v components). "
            "exp12: pressure + time bitwise; the rest differs only by the "
            "per-request GRIB re-packing + the q epsilon derivation (same "
            "causes as CARRA). LONGWAVE CAVEAT: the native handler requests "
            "the CARRA-style CDS name 'thermal_surface_radiation_downwards', "
            "which CDS silently drops for CERRA, so native CERRA acquisition "
            "cannot complete at all on the validated SYMFLUENCE branch; CFS "
            "requests the correct 'surface_thermal_radiation_downwards' and "
            "delivers all 7 variables (community-only, no native reference "
            "for longwave). Europe-only; archive ENDS 2021-06-30."
        ),
    ),
    DatasetSpec(
        family="HRRR",
        dataset_ids=("HRRR",),
        product="hrrr:sfc_anl",
        grid="projected",
        variables=(_STANDARD_8 - {"precipitation_flux"}) | {"wind_speed"},
        fetchable=_STANDARD_8 - {"precipitation_flux"},
        auth=frozenset(),
        temporal=("2016-08-24", "2100-01-01"),
        spatial=(-134.1, 21.1, -60.9, 52.6),
        parity="bit-identical",
        variables_key="HRRR_VARS",
        native_to_canonical=_HRRR_TO_CANONICAL,
        notes=(
            "NOAA HRRR analysis stream (public hrrrzarr S3 archive — the "
            "same per-variable Zarr groups the native handler reads), hourly "
            "3 km LCC y/x grid. exp14: all 7 variables + time bitwise on the "
            "overlaid window (same float16 chunks upcast to float32). The "
            "2-D lat/lon differ <= 3.9e-6 deg (~0.4 m): the native handler "
            "RECOMPUTES them with pyproj while CFS reads the archive's "
            "published grid arrays. The analysis stream carries NO "
            "precipitation on either side (CFS's separate hrrr:sfc_fcst "
            "product stays unclaimed). Native-side note: the native bbox "
            "windowing no-ops (the variable groups carry no latitude "
            "coordinate), so native downloads full CONUS (~1.3 GB/day) where "
            "the community fetch windows server-cheaply. CONUS only; upper "
            "coverage bound is nominal (rolling archive)."
        ),
    ),
    DatasetSpec(
        family="DAYMET",
        dataset_ids=("DAYMET",),
        product="daymet:daily_v4",
        grid="projected",
        variables=frozenset({
            "air_temperature", "precipitation_flux",
            "surface_downwelling_shortwave_flux",
        }),
        fetchable=frozenset({
            "air_temperature", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "dewpoint_temperature",
        }),
        auth=frozenset({"earthdata"}),
        temporal=("1980-01-01", "2024-01-01"),
        spatial=(-179.0, 14.0, -52.0, 83.0),
        parity="bit-identical:point-sampled",
        variables_key="DAYMET_VARIABLES",
        native_to_canonical=_DAYMET_TO_CANONICAL,
        notes=(
            "ORNL Daymet V4R1 DAILY 1 km LCC (NASA Hyrax OPeNDAP — the same "
            "granules the native handler targets). Canonical fields are "
            "derived from the raw daily variables with the documented "
            "formulas (T = (tmax+tmin)/2 + 273.15; precip flux = prcp/86400; "
            "SW = srad*dayl/86400 daily mean; dewpoint = inverse-Bolton(vp)). "
            "North America only; daily noon-anchored time axis (the "
            "canonical-v1 handler keeps it daily). Declared coverage ends at "
            "the last live-verified complete year (2023); the archive is "
            "extended annually — bump after verification. exp16: all four "
            "derivations recomputed in float32 from the raw values served by "
            "ORNL's single-pixel API (the native handler's own point route) "
            "are BITWISE identical to the canonical artifact at every "
            "sampled cell, with exact containing-cell LCC identity; the "
            "full-grid raw comparison was blocked by a NASA Hyrax outage on "
            "campaign day (hence the point-sampled qualifier). NATIVE-SIDE "
            "NOTE: the native gridded OPeNDAP route slices the descending "
            "Daymet y axis with an ascending slice and returns empty "
            "subsets — it cannot produce gridded data at all on the "
            "validated branch (and has no THREDDS-NCSS alternative)."
        ),
    ),
    DatasetSpec(
        family="CFS",
        dataset_ids=("CFS",),
        product=None,  # from options={'product': ...} or the CFS_PRODUCT key
        grid="regular_latlon",
        variables=_STANDARD_8 | {"wind_speed"},
        fetchable=frozenset(),  # unknown until the product is named
        auth=frozenset(),
        temporal=None,
        spatial=None,
        parity=None,  # UNGATED by design: any CFS product, no parity claim
        variables_key="CFS_VARIABLES",
        native_to_canonical=None,
        notes=(
            "Parallel-name access to the whole CFS catalog: pass the product as "
            "options={'product': 'provider:product'} or set the CFS_PRODUCT key "
            "(optional CFS_VARIABLES / CFS_CONNECTOR_CONFIG). Grid class varies "
            "by product. Ungraded (parity_grade None): the framework refuses it "
            "unless ALLOW_UNGATED_BACKENDS is true (UNKNOWN posture, so the "
            "posture-only gate does not admit this generic passthrough)."
        ),
    ),
    # ---- Posture-only expansion tier (contract 0.4.0) ---------------------
    # Live national/global forcing products with NO native pipeline to parity-
    # grade against. Ungraded (parity=None) but each carries an open/attribution
    # source licence (see _FORCING_POSTURE), so SYMFLUENCE's posture-only forcing
    # gate admits them. Every entry was live round-trip verified (a real
    # bbox+time fetch returned data); auth-gated / unverifiable products
    # (wfde5/CDS, barra2/NCI, em_earth/FRDR, gefs, cmorph, chirts,
    # nwm_operational) are deliberately NOT listed yet. All deliver
    # regular_latlon (EPSG:4326).
    DatasetSpec(
        family="GFS", dataset_ids=("GFS",), product="gfs:forecast_0p25",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        auth=frozenset(), temporal=None, spatial=None, parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NOAA GFS 0.25deg global forecast (NODD anonymous). Recent forecast horizon only.",
    ),
    DatasetSpec(
        family="CFSV2", dataset_ids=("CFSV2",), product="cfsv2:cdas_flux",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        auth=frozenset(), temporal=None, spatial=None, parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NCEP CFSv2/CDAS ~0.5deg global analysis stream (NODD anonymous). Recent horizon only.",
    ),
    DatasetSpec(
        family="W5E5", dataset_ids=("W5E5", "W5E5v2.0"), product="w5e5:obsclim_daily",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "wind_speed", "precipitation_flux", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "wind_speed", "precipitation_flux", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux"}),
        auth=frozenset(), temporal=("1979-01-01", "2020-01-01"), spatial=None, parity=None,
        variables_key=None, native_to_canonical=None,
        notes="W5E5 v2.0 bias-corrected global 0.5deg daily (ISIMIP, CC0). Wind is scalar (wind_speed).",
    ),
    DatasetSpec(
        family="LIVNEH", dataset_ids=("LIVNEH",), product="livneh:daily",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "precipitation_flux", "wind_speed"}),
        fetchable=frozenset({"air_temperature", "precipitation_flux", "wind_speed"}),
        auth=frozenset(), temporal=None, spatial=(-124.59, 25.16, -67.03, 52.84), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="Livneh CONUS+ 1/16deg daily (NOAA PSL, US-PD). Partial: precip/air_temp/wind only.",
    ),
    DatasetSpec(
        family="MRMS", dataset_ids=("MRMS", "MRMS_QPE"), product="mrms:multisensor_qpe_01h",
        grid="regular_latlon",
        variables=frozenset({"precipitation_flux"}),
        fetchable=frozenset({"precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-130.0, 20.0, -60.0, 55.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NOAA MRMS MultiSensor QPE 1 km CONUS hourly (NODD, US-PD). Precip only; rolling ~5.6yr archive.",
    ),
    DatasetSpec(
        family="GLDAS", dataset_ids=("GLDAS",), product="gldas:noah025_3h",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "wind_speed", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "precipitation_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "wind_speed", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-180.0, -60.0, 180.0, 90.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NASA GLDAS-2 Noah 0.25deg 3-hourly (GES DISC). Land 60S-90N.",
    ),
    DatasetSpec(
        family="FLDAS", dataset_ids=("FLDAS",), product="fldas:noah_global_monthly",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "wind_speed", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "precipitation_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "wind_speed", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-180.0, -60.0, 180.0, 90.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NASA FLDAS Noah global monthly (GES DISC). Land 60S-90N.",
    ),
    DatasetSpec(
        family="MERRA2", dataset_ids=("MERRA2", "MERRA-2"), product="merra2:single_levels",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "surface_air_pressure", "specific_humidity",
            "eastward_wind", "northward_wind", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "precipitation_flux"}),
        fetchable=frozenset({"air_temperature", "surface_air_pressure", "specific_humidity",
            "eastward_wind", "northward_wind", "surface_downwelling_shortwave_flux",
            "surface_downwelling_longwave_flux", "precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=None, parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NASA MERRA-2 single-level hourly reanalysis (GES DISC), global.",
    ),
    DatasetSpec(
        family="GPM", dataset_ids=("GPM", "GPM_IMERG", "IMERG"), product="gpm:imerg_daily",
        grid="regular_latlon",
        variables=frozenset({"precipitation_flux"}),
        fetchable=frozenset({"precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=None, parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NASA GPM IMERG daily precipitation (GES DISC), global.",
    ),
    DatasetSpec(
        family="ECMWF", dataset_ids=("ECMWF", "ECMWF_OPENDATA"), product="ecmwf_opendata:ifs_0p25",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "eastward_wind",
            "northward_wind", "surface_air_pressure", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        fetchable=frozenset({"air_temperature", "eastward_wind",
            "northward_wind", "surface_air_pressure", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        auth=frozenset(), temporal=None, spatial=None, parity=None,
        variables_key=None, native_to_canonical=None,
        notes="ECMWF IFS 0.25deg open-data forecast (CC-BY-4.0). Recent forecast horizon only.",
    ),
    DatasetSpec(
        family="NARR", dataset_ids=("NARR",), product="narr:daily",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity", "surface_air_pressure",
            "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        auth=frozenset(), temporal=None, spatial=(-170.0, 1.0, -20.0, 85.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NOAA NARR daily North American regional reanalysis (NCEI/PSL), regridded to lat-lon.",
    ),
    DatasetSpec(
        family="NAM", dataset_ids=("NAM",), product="nam:awphys_fcst",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity",
            "surface_air_pressure", "eastward_wind", "northward_wind",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux",
            "precipitation_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity",
            "surface_air_pressure", "eastward_wind", "northward_wind",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux",
            "precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-152.0, 12.0, -49.0, 61.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NOAA NAM 12 km North American forecast (NODD). Recent forecast horizon only.",
    ),
    DatasetSpec(
        family="RAP", dataset_ids=("RAP",), product="rap:awp130_fcst",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "specific_humidity",
            "surface_air_pressure", "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        fetchable=frozenset({"air_temperature", "specific_humidity",
            "surface_air_pressure", "eastward_wind", "northward_wind", "precipitation_flux",
            "surface_downwelling_shortwave_flux", "surface_downwelling_longwave_flux"}),
        auth=frozenset(), temporal=None, spatial=(-139.0, 16.0, -57.0, 58.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NOAA RAP 13 km North American forecast (NODD). Recent forecast horizon only.",
    ),
    DatasetSpec(
        family="GRIDMET", dataset_ids=("GRIDMET",), product="gridmet:daily",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "precipitation_flux", "specific_humidity",
            "wind_speed", "surface_downwelling_shortwave_flux"}),
        fetchable=frozenset({"air_temperature", "precipitation_flux", "specific_humidity",
            "wind_speed", "surface_downwelling_shortwave_flux"}),
        auth=frozenset(), temporal=None, spatial=(-125.0, 25.0, -66.0, 53.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="gridMET 4 km daily CONUS (University of Idaho, CC0).",
    ),
    DatasetSpec(
        family="NCLIMGRID", dataset_ids=("NCLIMGRID", "NCLIMGRID_DAILY"), product="nclimgrid_daily:daily",
        grid="regular_latlon",
        variables=frozenset({"air_temperature", "precipitation_flux"}),
        fetchable=frozenset({"air_temperature", "precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-125.0, 24.0, -66.0, 50.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="NOAA NCEI nClimGrid-daily CONUS (public domain).",
    ),
    DatasetSpec(
        family="PERSIANN-CDR", dataset_ids=("PERSIANN-CDR", "PERSIANN_CDR"), product="persiann_cdr:daily",
        grid="regular_latlon",
        variables=frozenset({"precipitation_flux"}),
        fetchable=frozenset({"precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-180.0, -60.0, 180.0, 60.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="PERSIANN-CDR daily precipitation 60S-60N (NOAA NCEI distribution, public domain).",
    ),
    DatasetSpec(
        family="CHIRPS", dataset_ids=("CHIRPS",), product="chirps:daily_p05",
        grid="regular_latlon",
        variables=frozenset({"precipitation_flux"}),
        fetchable=frozenset({"precipitation_flux"}),
        auth=frozenset(), temporal=None, spatial=(-180.0, -50.0, 180.0, 50.0), parity=None,
        variables_key=None, native_to_canonical=None,
        notes="CHIRPS v2 0.05deg daily precipitation 50S-50N land (UCSB Climate Hazards Center).",
    ),
)


def _spec_for(dataset_id: str) -> DatasetSpec | None:
    wanted = dataset_id.lower()
    for spec in DATASET_SPECS:
        if any(wanted == did.lower() for did in spec.dataset_ids):
            return spec
    return None


# ── Shared helpers ───────────────────────────────────────────────────


def _netcdf_encoding(ds: xr.Dataset) -> dict[str, dict[str, Any]]:
    """Compressed per-variable encoding for ``ds.to_netcdf(encoding=...)``."""
    return {str(name): {"zlib": True, "complevel": 1} for name in ds.data_vars}


def _product_tag(product: str) -> str:
    """Filesystem-safe tag for a CFS product id (``aorc:conus_1km`` → ``aorc_conus_1km``)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", product).strip("_").lower()


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    """Flat config read working for dicts and SymfluenceConfig-like objects."""
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        value = getter(key, default)
        return default if value is None else value
    return default


def _as_list(value: Any) -> list[str] | None:
    """Coerce a config value (comma-string or list) into a list of names."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
        return items or None
    return [str(v) for v in value] or None


def _derive_wind_speed(ds: xr.Dataset) -> xr.Dataset:
    """Derive ``wind_speed`` from the wind primitives when absent.

    Uses the same float32 op order as SYMFLUENCE's native ERA5 derivation:
    ``((u**2 + v**2) ** 0.5).astype(float32)``. For RDRS/CaSR this composite
    deviates <= 9e-4 m/s (max, exp10 measurement: 8.8e-4) from CaSR's own
    ``sfcWind`` diagnostic — CaSR computes its wind speed upstream with
    different physics-level rounding. Physically negligible; documented here
    and in docs/symfluence.md rather than chased.
    """
    if "wind_speed" in ds.data_vars or not (
        {"eastward_wind", "northward_wind"} <= set(ds.data_vars)
    ):
        return ds
    u, v = ds["eastward_wind"], ds["northward_wind"]
    wind = ((u**2 + v**2) ** 0.5).astype("float32")
    wind.attrs = {"units": "m s-1", "long_name": "wind speed", "standard_name": "wind_speed"}
    ds["wind_speed"] = wind
    return ds


def _add_native_era5_derivations(ds: xr.Dataset) -> xr.Dataset:
    """Derive ``wind_speed`` / ``specific_humidity`` exactly like native SYMFLUENCE.

    The native ERA5 acquisition pipeline (``handlers/era5_processing.py``)
    ships these two derived variables in its raw files; CFS's ``era5_arco``
    product ships only the primitives (u/v wind, dewpoint, pressure). To keep
    the community files drop-in equivalent, the backend recomputes them with
    the **same float32 op order as SYMFLUENCE** — live-verified bitwise equal
    to the native output (2026-06-11 parity experiments). Do not "improve"
    these formulas: bit-parity with the native pipeline is the contract.
    """
    import numpy as np
    import xarray

    ds = _derive_wind_speed(ds)
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


# ── The community acquisition backend ────────────────────────────────


class CommunityForcingBackend:
    """CFS as a SYMFLUENCE ``AcquisitionBackend`` (the ``community`` backend).

    Instantiated by the framework's selection layer with ``(config, logger)``,
    exactly like the in-tree ``NativeBackend``. ``capabilities()`` is static
    (the parity-gated :data:`DATASET_SPECS` table); ``acquire()`` translates
    the protocol request into :func:`cfs.fetch_sync` calls, writes canonical-v1
    NetCDF plus the sidecar acquisition manifest into ``request.target_dir``,
    and maps CFS-internal failures onto the protocol error taxonomy.
    """

    name = "community"
    interface_version = TARGET_INTERFACE_VERSION

    def __init__(self, config: Any = None, logger: Any = None) -> None:
        self.config = config
        self.logger = logger or _fallback_logger()

    # -- protocol surface --------------------------------------------------

    def capabilities(self) -> tuple[Any, ...]:
        """One :class:`DatasetCapability` per requestable dataset id."""
        contract = _require_contract()
        caps = []
        for spec in DATASET_SPECS:
            redis, lic, attribution = _FORCING_POSTURE.get(spec.family, ("unknown", "", ""))
            for dataset_id in spec.dataset_ids:
                caps.append(contract.DatasetCapability(
                    dataset_id=dataset_id,
                    grid_class=contract.GridClass(spec.grid),
                    schema=contract.SchemaId.CANONICAL_V1,
                    variables=spec.variables,
                    temporal=spec.temporal,
                    auth=spec.auth,
                    parity_grade=spec.parity,
                    notes=spec.notes,
                    redistribution=contract.Redistribution(redis),
                    data_license=lic,
                    attribution=attribution,
                ))
        return tuple(caps)

    def acquire(self, request: Any) -> Any:
        """Serve an :class:`AcquisitionRequest` via :func:`cfs.fetch_sync`."""
        contract = _require_contract()
        spec = _spec_for(request.dataset_id)
        if spec is None:
            raise _errors.DatasetUnsupported(
                f"The community backend does not serve dataset '{request.dataset_id}' "
                f"(served: {sorted(did for s in DATASET_SPECS for did in s.dataset_ids)})",
                dataset_id=request.dataset_id,
                backend=self.name,
            )

        target_dir = Path(request.target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        window = self._window(request, spec)
        bbox = self._bbox(request)
        self._check_spatial(spec, bbox)

        if spec.family == "NEX-GDDP":
            paths, delivered, warnings, provenance = self._acquire_nex(request, spec, target_dir, window, bbox)
        else:
            paths, delivered, warnings, provenance = self._acquire_product(
                request, spec, target_dir, window, bbox
            )

        result = contract.AcquisitionResult(
            paths=tuple(paths),
            schema=contract.SchemaId.CANONICAL_V1,
            dataset_id=request.dataset_id,
            backend=self.name,
            provenance=provenance,
            variables_delivered=frozenset(delivered),
            warnings=tuple(warnings),
        )
        contract.write_manifest(result, target_dir)
        return result

    # -- single-product acquisition ----------------------------------------

    def _acquire_product(self, request, spec, target_dir, window, bbox):
        product, connector_config = self._resolve_product(request, spec)
        variables = self._fetch_variables(request, spec)
        out_path = target_dir / self._output_name(product, window)

        if out_path.exists() and not self._force_download():
            self.logger.info(
                f"{spec.family}: output already exists, skipping fetch: {out_path.name}"
            )
            delivered = sorted(self._delivered_from_file(out_path))
            provenance = self._provenance(product, "reused existing output (skip-if-exists)")
            return [out_path], delivered, [], provenance

        self.logger.info(
            f"{spec.family}: acquiring via CFS (product '{product}', bbox {bbox}, "
            f"{window[0]} to {window[1]})" + (f", variables: {variables}" if variables else "")
        )
        ds, fetch_result = self._fetch(
            spec, product, bbox, window, variables, config=connector_config
        )
        if spec.family == "ERA5":
            ds = _add_native_era5_derivations(ds)

        ds.to_netcdf(out_path, encoding=_netcdf_encoding(ds))
        delivered = sorted(str(name) for name in ds.data_vars)
        ds.close()

        self.logger.info(
            f"{spec.family} community acquisition complete: {out_path.name} "
            f"({fetch_result.n_times} timesteps, {fetch_result.n_lat}x{fetch_result.n_lon} cells)"
        )
        provenance = self._provenance(product, fetch_result.provenance, fetch_result)
        return [out_path], delivered, list(fetch_result.warnings), provenance

    # -- NEX-GDDP (product built per scenario from the native config keys) ---

    def _acquire_nex(self, request, spec, target_dir, window, bbox):
        """One canonical NetCDF per model x scenario x member combination."""
        from cfs.connectors.nex_gddp import SCENARIOS

        options = dict(request.options or {})
        models = _as_list(options.get("models") or _cfg(self.config, "NEX_MODELS"))
        if not models:
            raise _errors.AcquisitionError(
                "NEX-GDDP (community backend): NEX_MODELS must be set "
                "(or pass options={'models': [...]})."
            )
        scenarios = _as_list(options.get("scenarios") or _cfg(self.config, "NEX_SCENARIOS")) or ["historical"]
        members = _as_list(options.get("members") or _cfg(self.config, "NEX_ENSEMBLES")) or ["r1i1p1f1"]
        variables = self._fetch_variables(request, spec)

        start, end = window
        paths: list[Path] = []
        delivered: set[str] = set()
        warnings: list[str] = []
        provenance: dict[str, str] = {}

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
                    product = f"nex_gddp:{scenario}"
                    tag = _product_tag(f"nex_gddp_{scenario}_{model}_{member}")
                    out_path = target_dir / self._output_name(tag, (window_start, window_end), tagged=True)
                    if out_path.exists() and not self._force_download():
                        paths.append(out_path)
                        delivered |= self._delivered_from_file(out_path)
                        continue
                    self.logger.info(
                        f"NEX-GDDP: acquiring via CFS ({product}, model={model}, member={member})"
                    )
                    ds, fetch_result = self._fetch(
                        spec, product, bbox, (window_start, window_end), variables,
                        config={"model": str(model), "member": str(member)},
                    )
                    ds = self._nex_add_synthetic_pressure(ds)
                    ds.to_netcdf(out_path, encoding=_netcdf_encoding(ds))
                    delivered |= {str(name) for name in ds.data_vars}
                    ds.close()
                    paths.append(out_path)
                    warnings.extend(fetch_result.warnings)
                    provenance.setdefault("provider", fetch_result.provider)
                    provenance[f"{tag}"] = fetch_result.provenance or product

        if not paths:
            raise _errors.WindowOutOfRange(
                "NEX-GDDP-CMIP6 (community backend): no data acquired — every "
                "model/scenario combination fell outside its scenario extent.",
            )
        provenance.setdefault("acquired_at", datetime.now(UTC).isoformat())
        provenance.setdefault("backend", "cfs")
        self.logger.info(f"NEX-GDDP community acquisition complete: {len(paths)} file(s)")
        return paths, sorted(delivered), warnings, provenance

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

        elev_cfg = _cfg(self.config, "DOMAIN_MEAN_ELEV_M")
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
                "Constant p0*exp(-z/H) fabricated by the CFS community backend; "
                "NEX-GDDP-CMIP6 publishes no surface pressure (mirrors the native handler)."
            ),
        }
        ds["surface_air_pressure"] = pressure
        return ds

    # -- request resolution helpers ------------------------------------------

    def _resolve_product(self, request, spec) -> tuple[str, dict | None]:
        """Resolve the CFS product id (+ connector config) for a request."""
        if spec.family == "CARRA":
            # The CDS Arctic split follows the native CARRA_DOMAIN key
            # (west_domain default — the connector's own default too).
            options = dict(request.options or {})
            domain = options.get("domain") or _cfg(self.config, "CARRA_DOMAIN")
            return cast(str, spec.product), ({"domain": str(domain)} if domain else None)
        if spec.product is not None:
            return spec.product, None
        # Parallel-name 'CFS' entry: product from options or the legacy key.
        options = dict(request.options or {})
        product = options.get("product") or _cfg(self.config, "CFS_PRODUCT")
        if not product:
            raise _errors.AcquisitionError(
                "Dataset 'CFS' needs a product: pass options={'product': "
                "'provider:product'} or set the CFS_PRODUCT config key "
                "(e.g. 'gefs:atmos_0p25')."
            )
        connector_config = options.get("connector_config") or _cfg(self.config, "CFS_CONNECTOR_CONFIG")
        return str(product), connector_config

    def _window(self, request, spec) -> tuple[datetime, datetime]:
        """Resolve and validate the request window against declared coverage."""
        if not request.window:
            raise _errors.AcquisitionError(
                f"{spec.family}: the community backend requires an explicit "
                "acquisition window (request.window is None)."
            )
        try:
            start = datetime.fromisoformat(str(request.window[0]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(request.window[1]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise _errors.AcquisitionError(
                f"{spec.family}: unparseable window {request.window!r}: {exc}"
            ) from exc
        # Compare on naive UTC so 'Z'-suffixed and naive config times mix.
        start, end = (t.replace(tzinfo=None) for t in (start, end))
        if spec.temporal is not None:
            cov_start = datetime.fromisoformat(spec.temporal[0])
            cov_end = datetime.fromisoformat(spec.temporal[1])
            if start < cov_start or end > cov_end:
                raise _errors.WindowOutOfRange(
                    f"{spec.family}: window [{start}, {end}] is outside the "
                    f"community backend's coverage [{spec.temporal[0]}, "
                    f"{spec.temporal[1]}) — the native backend serves it.",
                    coverage=spec.temporal,
                )
        return start, end

    def _check_spatial(self, spec, bbox: tuple[float, float, float, float]) -> None:
        """Refuse bboxes outside a regional dataset's spatial domain.

        The domain limit is a property of the *dataset* (CARRA data simply
        does not exist south of the Arctic), not of this backend — no backend
        can serve such a request, so this raises a plain ``AcquisitionError``
        rather than a decline-and-fallback class. The contract's
        ``DatasetCapability`` has no spatial field yet, so the check has to
        live here at ``acquire()`` time.
        """
        if spec.spatial is None:
            return
        west, south, east, north = spec.spatial
        req_w, req_s, req_e, req_n = bbox
        if req_w < west or req_e > east or req_s < south or req_n > north:
            raise _errors.AcquisitionError(
                f"{spec.family}: bbox (W={req_w}, S={req_s}, E={req_e}, N={req_n}) is "
                f"outside the dataset's spatial domain (W={west}, S={south}, E={east}, "
                f"N={north}). This is a property of the dataset itself, not of the "
                "community backend — no backend can serve it; choose a dataset that "
                "covers your domain."
            )

    def _bbox(self, request) -> tuple[float, float, float, float]:
        """Contract bbox (lat_min, lon_min, lat_max, lon_max) -> CFS (W, S, E, N)."""
        if not request.bbox:
            raise _errors.AcquisitionError(
                "The community backend requires a bounding box "
                "(set BOUNDING_BOX_COORDS: 'north/west/south/east')."
            )
        lat_min, lon_min, lat_max, lon_max = (float(v) for v in request.bbox)
        return (lon_min, lat_min, lon_max, lat_max)

    def _fetch_variables(self, request, spec) -> list[str] | None:
        """Canonical fetch list for the request (``None`` = product default).

        Priority: explicit ``request.variables`` (CFIF names) over the legacy
        per-dataset config key (native names, translated). Derived variables
        (``wind_speed`` everywhere, ERA5 ``specific_humidity``) are replaced by
        their primitives in the fetch list; unknown names are dropped loudly.
        """
        requested: list[str] | None = None
        if request.variables:
            requested = sorted(str(v) for v in request.variables)
        elif spec.variables_key:
            native = _as_list(_cfg(self.config, spec.variables_key))
            if native:
                mapping = spec.native_to_canonical or {}
                canonical_names = set(CFS_TO_CFIF_MAPPING) | {"dewpoint_temperature"}
                requested, unmapped = [], []
                for name in native:
                    target = mapping.get(name) or (name if name in canonical_names else None)
                    if target is None:
                        unmapped.append(name)
                    elif target not in requested:
                        requested.append(target)
                if unmapped:
                    self.logger.warning(
                        f"{spec.family}: requested {spec.variables_key} variable(s) with no "
                        f"canonical-v1 counterpart are NOT included in the community fetch: "
                        f"{', '.join(unmapped)}. Pin {spec.dataset_ids[0].upper()}_BACKEND: "
                        "native if you need them."
                    )
                if not requested:
                    raise _errors.AcquisitionError(
                        f"{spec.family}: none of the requested {spec.variables_key} variables "
                        f"({', '.join(native)}) map to canonical-v1 names."
                    )
        if requested is None:
            return None
        if not spec.fetchable:  # parallel-name 'CFS': pass through untouched
            return requested
        fetch: list[str] = [v for v in requested if v in spec.fetchable]
        if "wind_speed" in requested and "wind_speed" not in spec.fetchable:
            fetch.extend(v for v in ("eastward_wind", "northward_wind") if v not in fetch)
        if (
            spec.family == "ERA5"
            and "specific_humidity" in requested
        ):
            fetch.extend(
                v for v in ("dewpoint_temperature", "surface_air_pressure") if v not in fetch
            )
        return fetch or None

    # -- mechanics -----------------------------------------------------------

    def _fetch(self, spec, product, bbox, window, variables, config=None):
        """Call ``cfs.fetch_sync``, mapping CFS failures onto the protocol taxonomy."""
        import cfs
        from cfs.core import exceptions as cfs_exc

        try:
            return cfs.fetch_sync(
                product, bbox=bbox, time_range=window, variables=variables, config=config
            )
        except cfs_exc.RegistrationRequiredError as exc:
            raise _errors.AuthRequired(
                f"{spec.family}: upstream provider requires credentials: {exc}",
                provider=getattr(exc, "provider", ""),
                howto=getattr(exc, "instructions", "") or getattr(exc, "registration_url", ""),
            ) from exc
        except cfs_exc.SubsetError as exc:
            raise _errors.WindowOutOfRange(
                f"{spec.family}: requested subset is empty: {exc}",
                coverage=spec.temporal,
            ) from exc
        except (cfs_exc.DataFormatError, cfs_exc.HarmonizationError) as exc:
            raise _errors.IntegrityError(
                f"{spec.family}: upstream delivered unusable data: {exc}"
            ) from exc
        except (cfs_exc.RateLimitError, cfs_exc.ProtocolError, cfs_exc.ConnectorError) as exc:
            raise _errors.UpstreamOutage(
                f"{spec.family}: upstream failure while fetching '{product}': {exc}",
                upstream=product,
            ) from exc
        except cfs_exc.CFSError as exc:
            raise _errors.AcquisitionError(
                f"{spec.family}: CFS fetch of '{product}' failed: {exc}"
            ) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise _errors.UpstreamOutage(
                f"{spec.family}: network failure while fetching '{product}': {exc}",
                upstream=product,
            ) from exc

    def _output_name(self, product_or_tag: str, window, *, tagged: bool = False) -> str:
        """Canonical output filename: ``domain_{name}_cfs_{tag}_{start}_{end}.nc``."""
        tag = product_or_tag if tagged else _product_tag(product_or_tag)
        domain = str(_cfg(self.config, "DOMAIN_NAME", "domain"))
        start_str = window[0].strftime("%Y%m%d")
        end_str = window[1].strftime("%Y%m%d")
        return f"domain_{domain}_cfs_{tag}_{start_str}_{end_str}.nc"

    def _force_download(self) -> bool:
        value = _cfg(self.config, "FORCE_DOWNLOAD", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _delivered_from_file(path: Path) -> set[str]:
        """Variable names in an existing output (skip-if-exists honesty)."""
        import xarray

        with xarray.open_dataset(path) as ds:
            return {str(name) for name in ds.data_vars}

    @staticmethod
    def _provenance(product: str, detail: str, fetch_result=None) -> dict[str, str]:
        prov = {
            "backend": "cfs",
            "product_id": product,
            "detail": str(detail or ""),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        if fetch_result is not None:
            prov["provider"] = fetch_result.provider
            prov["elapsed_ms"] = str(fetch_result.elapsed_ms)
        return prov


def _require_contract():
    if _contract is None:
        raise RuntimeError(
            "The SYMFLUENCE AcquisitionBackend contract is unavailable "
            "(symfluence is not installed); CommunityForcingBackend cannot operate."
        )
    return _contract


def _fallback_logger():
    import logging

    return logging.getLogger("cfs.integrations.symfluence")


# ── The schema-keyed canonical-v1 dataset handler ────────────────────


class CanonicalV1Handler(_DatasetBase):
    """SYMFLUENCE preprocessing handler for canonical-v1 raw files (ANY dataset).

    Registered ONCE under the schema key ``canonical-v1``; SYMFLUENCE resolves
    it from the acquisition manifest's declared schema, so there is exactly one
    canonical->CFIF pathway instead of per-dataset self-detecting shadows.

    Canonical-v1 data are CF-aligned SI fluxes — identical conventions to
    CFIF — so :meth:`process_dataset` performs an (identity) rename, derives
    ``wind_speed`` from the wind primitives when absent (the one CFIF variable
    several canonical products do not ship), and applies the standard CFIF
    attributes. The native per-dataset unit heuristics are deliberately
    SKIPPED: canonical data are already SI by contract.

    Both canonical grid classes are supported:

    * **regular_latlon** — 1-D ``latitude``/``longitude`` dimension coords;
      merge/shapefile delegate to the proven regular-grid implementations.
    * **projected** (rotated-pole / LCC; RDRS first) — native ``rlat``/``rlon``
      or ``y``/``x`` dims with 2-D ``latitude``/``longitude`` coordinates. The
      consolidated canonical store is split into native-pipeline-style monthly
      files (``{DATASET}_monthly_YYYYMM.nc``) and the forcing-grid shapefile is
      built per-cell from the 2-D coordinate corners (ported from the proven
      native RDRS implementation — exp10 verified the grids bitwise identical,
      so the geometry matches).
    """

    # -- contract: naming / structure ----------------------------------------

    def get_variable_mapping(self) -> dict[str, str]:
        """Canonical-v1 names -> CFIF names (identity where they coincide)."""
        return dict(CFS_TO_CFIF_MAPPING)

    def get_coordinate_names(self) -> tuple[str, str]:
        """Canonical-v1 always names its geographic coordinates latitude/longitude.

        Regular grids carry them as 1-D dimension coordinates; projected grids
        carry them as 2-D auxiliary coordinates over the native dims (EASYMORE
        handles both by name).
        """
        return ("latitude", "longitude")

    def needs_merging(self) -> bool:
        """Standardize raw canonical files into the merged layout."""
        return True

    def get_merged_file_pattern(self, year: int, month: int) -> str:
        """Native-pipeline-style monthly name (``RDRS_monthly_201806.nc``)."""
        return f"{self._dataset_prefix()}_monthly_{year}{month:02d}.nc"

    def _dataset_prefix(self) -> str:
        dataset = self._get_config_value(
            lambda: self.config.forcing.dataset, default="CFS", dict_key="FORCING_DATASET"
        )
        return re.sub(r"[^A-Za-z0-9]+", "_", str(dataset)).strip("_").upper() or "CFS"

    @staticmethod
    def _is_projected(ds: xr.Dataset) -> bool:
        """Projected canonical layout: 2-D latitude over native (rlat/rlon, y/x) dims."""
        return "latitude" in ds.coords and getattr(ds["latitude"], "ndim", 1) == 2

    # -- contract: per-dataset processing ------------------------------------

    def process_dataset(self, ds: xr.Dataset) -> xr.Dataset:
        """Rename canonical variables to CFIF, derive wind_speed, apply attributes.

        No unit conversion and none of the native handlers' unit heuristics:
        canonical-v1 guarantees SI units and fluxes (never accumulations).
        """
        renames = {
            old: new for old, new in CFS_TO_CFIF_MAPPING.items()
            if old in ds.variables and new != old
        }
        if renames:  # pragma: no cover - identity mapping today; future-proofing
            ds = ds.rename(renames)
        ds = _derive_wind_speed(ds)
        return cast("xr.Dataset", self.apply_standard_attributes(ds, overrides={
            "precipitation_flux": {"units": "kg m-2 s-1", "standard_name": "precipitation_rate"}
        }))

    # -- contract: merge ------------------------------------------------------

    def merge_forcings(
        self, raw_forcing_path: Path, merged_forcing_path: Path, start_year: int, end_year: int
    ) -> None:
        """Standardize raw canonical files into the merged layout (grid-aware).

        Regular grids: per-file standardization via the generic regular-grid
        loop (raw filenames preserved). Projected grids: the consolidated
        canonical store is processed and split into monthly files exactly where
        the native projected pipeline expects them.
        """
        import xarray

        raw_forcing_path = Path(raw_forcing_path)
        merged_forcing_path = Path(merged_forcing_path)
        merged_forcing_path.mkdir(parents=True, exist_ok=True)

        files = sorted(raw_forcing_path.glob("*.nc"))
        files = [f for f in files if self._file_overlaps_period(f, start_year, end_year)]
        if not files:
            self.logger.warning(f"No canonical raw files found in {raw_forcing_path}")
            return

        with xarray.open_dataset(files[0]) as probe:
            projected = self._is_projected(probe)

        if projected:
            self._merge_projected(files, merged_forcing_path, start_year, end_year)
        else:
            # The ERA5 handler's merge loop is grid-agnostic for regular
            # lat/lon products (glob, self.process_dataset, atomic write).
            from symfluence.data.preprocessing.dataset_handlers.era5_utils import ERA5Handler

            ERA5Handler.merge_forcings(
                self, raw_forcing_path, merged_forcing_path, start_year, end_year
            )

    @staticmethod
    def _native_time_step(ds: xr.Dataset) -> Any:
        """Median time step of the store (``pandas.Timedelta``), or ``None``.

        Canonical stores are regular by contract, but a consolidated file can
        carry gaps — the *median* diff recovers the native step regardless
        (hourly for RDRS/CONUS404/HRRR/NWM3, daily for Daymet).
        """
        import numpy as np
        import pandas as pd

        if ds.sizes.get("time", 0) < 2:
            return None
        diffs = np.diff(ds["time"].values)
        return pd.Timedelta(np.median(diffs.astype("timedelta64[s]").astype("int64")), unit="s")

    def _merge_projected(
        self, files: list[Path], merged_forcing_path: Path, start_year: int, end_year: int
    ) -> None:
        """Split consolidated projected canonical store(s) into monthly files.

        Mirrors the native projected pipeline's consolidated-file path
        (rdrs_utils._merge_from_consolidated): per-month slice, complete time
        axis (gap interpolation + edge fill), standard time encoding and
        cleaned attributes, written as ``{DATASET}_monthly_YYYYMM.nc``.

        The native pipeline hardcodes an hourly axis (RDRS); here the axis is
        rebuilt at the store's *native* step so every projected grid family
        works: hourly stores keep the exact native behaviour (full-month
        00:00..23:00 axis), while non-hourly stores (Daymet daily, anchored at
        noon) are re-gridded from the first to the last actual timestamp of
        the month at their own step — never resampled to a finer resolution.
        """
        import pandas as pd
        import xarray

        self.logger.info(
            f"Merging {len(files)} consolidated canonical (projected-grid) file(s) "
            "into monthly files"
        )
        datasets = [self.process_dataset(self.open_dataset(f)) for f in files]
        ds = datasets[0] if len(datasets) == 1 else xarray.concat(
            datasets, dim="time", data_vars="all"
        ).sortby("time").drop_duplicates(dim="time")

        step = self._native_time_step(ds)
        hourly = step is not None and step == pd.Timedelta(hours=1)

        for year in range(start_year - 1, end_year + 1):
            for month in range(1, 13):
                start_time = pd.Timestamp(year, month, 1)
                if month == 12:
                    end_time = pd.Timestamp(year + 1, 1, 1) - pd.Timedelta(hours=1)
                else:
                    end_time = pd.Timestamp(year, month + 1, 1) - pd.Timedelta(hours=1)

                monthly = ds.sel(time=slice(str(start_time), str(end_time)))
                if monthly.sizes.get("time", 0) == 0:
                    continue

                expected_times: Any
                if hourly:
                    # Exact native-pipeline behaviour: full-month hourly axis.
                    expected_times = pd.date_range(start=start_time, end=end_time, freq="h")
                elif step is not None:
                    # Native-step axis anchored on the store's own timestamps
                    # (Daymet daily values sit at 12:00; keep that anchoring).
                    expected_times = pd.date_range(
                        start=monthly["time"].values[0],
                        end=monthly["time"].values[-1],
                        freq=step,
                    )
                else:  # single-step month: nothing to fill
                    expected_times = monthly["time"].values
                monthly = monthly.reindex(time=expected_times)
                monthly = monthly.interpolate_na(dim="time", method="linear")
                monthly = monthly.ffill(dim="time").bfill(dim="time")

                monthly = self.setup_time_encoding(monthly)
                monthly = self.add_metadata(
                    monthly,
                    "canonical-v1 projected store split into monthly files (CFS community backend)",
                )
                monthly = self.clean_variable_attributes(monthly)

                out = merged_forcing_path / self.get_merged_file_pattern(year, month)
                monthly.to_netcdf(out)
                self.logger.info(f"Saved monthly file: {out.name}")

        for d in datasets:
            d.close()

    # -- contract: forcing-grid shapefile -------------------------------------

    def create_shapefile(
        self, shapefile_path: Path, merged_forcing_path: Path, dem_path: Path, elevation_calculator
    ) -> Path:
        """Build the forcing-grid shapefile (grid-aware) for remapping weights."""
        import xarray

        merged_forcing_path = Path(merged_forcing_path)
        files = sorted(merged_forcing_path.glob("*.nc"))
        if not files:
            raise FileNotFoundError(
                f"No merged canonical forcing files found in {merged_forcing_path}"
            )
        with xarray.open_dataset(files[0]) as probe:
            projected = self._is_projected(probe)

        if projected:
            return self._create_projected_shapefile(
                shapefile_path, files[0], dem_path, elevation_calculator
            )

        from symfluence.data.preprocessing.dataset_handlers.era5_utils import ERA5Handler

        return cast(Path, ERA5Handler.create_shapefile(
            self, shapefile_path, merged_forcing_path, dem_path, elevation_calculator
        ))

    def _create_projected_shapefile(
        self, shapefile_path: Path, sample_file: Path, dem_path: Path, elevation_calculator
    ) -> Path:
        """Per-cell polygons from native dims + 2-D lat/lon corners.

        Ported from the proven native projected implementation
        (rdrs_utils.create_shapefile); the canonical grid was verified bitwise
        identical to the native grid (exp10), so the geometry matches. Corner
        (i, j) cells are clamped at the grid edge exactly like the native code.
        """
        import geopandas as gpd
        from shapely.geometry import Polygon

        self.logger.info("Creating projected-grid (canonical-v1) forcing shapefile")
        dataset = self._get_config_value(
            lambda: self.config.forcing.dataset, default="unknown", dict_key="FORCING_DATASET"
        )
        output_shapefile = Path(shapefile_path) / f"forcing_{dataset}.shp"

        with self.open_dataset(sample_file) as ds:
            lat = ds["latitude"].values
            lon = ds["longitude"].values
        n_y, n_x = lat.shape
        self.logger.info(f"Projected grid dimensions: {n_y} x {n_x}")

        # Corner fallbacks are the EXACT native shape (rdrs_utils): a missing
        # (i+1 / j+1) neighbour falls back to the (i, j) corner itself, so the
        # last row/column produce the same degenerate edge cells the native
        # shapefile carries (EASYMORE's intersection handles them identically).
        geometries, ids, lats, lons = [], [], [], []
        for i in range(n_y):
            for j in range(n_x):
                lat_corners = [
                    lat[i, j],
                    lat[i, j + 1] if j + 1 < n_x else lat[i, j],
                    lat[i + 1, j + 1] if i + 1 < n_y and j + 1 < n_x else lat[i, j],
                    lat[i + 1, j] if i + 1 < n_y else lat[i, j],
                ]
                lon_corners = [
                    lon[i, j],
                    lon[i, j + 1] if j + 1 < n_x else lon[i, j],
                    lon[i + 1, j + 1] if i + 1 < n_y and j + 1 < n_x else lon[i, j],
                    lon[i + 1, j] if i + 1 < n_y else lon[i, j],
                ]
                geometries.append(Polygon(zip(lon_corners, lat_corners)))
                ids.append(i * n_x + j)
                lats.append(float(lat[i, j]))
                lons.append(float(lon[i, j]))

        gdf = gpd.GeoDataFrame({
            "geometry": geometries,
            "ID": ids,
            self._get_config_value(
                lambda: self.config.forcing.shape_lat_name, default="lat",
                dict_key="FORCING_SHAPE_LAT_NAME",
            ): lats,
            self._get_config_value(
                lambda: self.config.forcing.shape_lon_name, default="lon",
                dict_key="FORCING_SHAPE_LON_NAME",
            ): lons,
        }, crs="EPSG:4326")

        self.logger.info("Calculating per-cell elevations")
        gdf["elev_m"] = elevation_calculator(gdf, dem_path, batch_size=50)

        if self._get_config_value(lambda: None, default=False, dict_key="REMOVE_INVALID_ELEVATION_CELLS"):
            valid_count = len(gdf)
            gdf = gdf[gdf["elev_m"] != -9999].copy()
            removed = valid_count - len(gdf)
            if removed:
                self.logger.info(f"Removed {removed} cells with invalid elevation values")

        output_shapefile.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(output_shapefile)
        self.logger.info(f"Projected-grid forcing shapefile saved: {output_shapefile}")
        return output_shapefile


# ── Registration ─────────────────────────────────────────────────────


def _register_impl() -> None:
    """Actual registration logic for :func:`register` (idempotent overwrite).

    Adds the community forcing backend to ``R.acquisition_backends`` — the
    registry SYMFLUENCE's forcing-selection layer consults (``select_backend``);
    the layer decides per request whether it serves. The protocol backend is
    deliberately NOT placed in ``R.acquisition_handlers`` (that registry holds
    legacy download/process handlers and is bridged to the protocol by the
    framework's own ``NativeBackend``; a protocol backend there serves nothing
    and would fail the legacy handler contract). The schema-keyed canonical-v1
    dataset handler registers under ``R.dataset_handlers`` (resolved from the
    acquisition manifest's declared schema). Raises ``ImportError`` when
    SYMFLUENCE is absent — discovery logs and skips a failing plugin.
    """
    from symfluence.core.registries import R

    R.acquisition_backends.add("community", CommunityForcingBackend)
    R.dataset_handlers.add("canonical-v1", CanonicalV1Handler)


# The module is now fully defined; register() may do its real work. This
# complements the entry point: if THIS module was imported before symfluence,
# the defensive import above already triggered symfluence's bootstrap, whose
# plugin discovery called register() while _MODULE_READY was False (a quiet
# no-op). Flipping the flag and registering here makes the backend available
# regardless of import order; _register_impl() is idempotent so the normal
# entry-point path stays harmless.
_MODULE_READY = True

if HAVE_SYMFLUENCE:  # pragma: no cover - exercised only with SYMFLUENCE present
    import contextlib

    # Never let registration break ``import cfs.integrations.symfluence``.
    with contextlib.suppress(Exception):
        register()
