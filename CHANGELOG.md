# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-06-11

### Added

- **Public API facade**: the blessed import surface is now re-exported from
  the top-level package with an explicit `cfs.__all__` — `discover`,
  `get_connector`, `list_providers`, `BoundingBox`, `TimeRange`,
  `CanonicalVar`, `FetchResult`, plus the new facade functions below.
  Connector modules remain lazy (imported only by `discover()`).
- **One-shot fetch**: `cfs.fetch(product_or_slug, bbox, time_range,
  variables=None, config=None)` runs discovery, connector resolution, and the
  async connector lifecycle in a single call. Accepts a full product ID
  (`"era5_arco:single_levels"`, the same identifier the CLI takes) or a bare
  provider slug when the provider offers exactly one product;
  bbox/time_range/variables accept plain tuples and strings as well as the
  typed models.
- **Sync wrapper**: `cfs.fetch_sync(...)` wraps `cfs.fetch` via
  `asyncio.run`, with a clear error when called from a running event loop.
- **`cfs.configure(**overrides)`**: programmatic runtime-settings hook for
  embedders — validates keywords against `Settings` fields, writes the
  corresponding `CFS_*` environment variables, and clears the
  `get_settings()` cache so overrides (cache dir, timeouts, guardrails)
  take effect after import.

## [0.1.0] — 2026-06-11

Initial release.

### Added

- **33 forcing connectors** (31 live-verified against their upstream stores;
  `mswep` and `em_earth` offline-verified pending access/credentials), spanning
  global and regional reanalyses (ERA5/ERA5-Land, MERRA-2, CARRA, CERRA, RDRS,
  BARRA2, CONUS404, NARR, WFDE5), analysis products (AORC, NLDAS, HRRR, NWM
  operational, Daymet, gridMET, nClimGrid-Daily, GLDAS, FLDAS), satellite and
  station precipitation/temperature (CHIRPS, CHIRTS, GPM IMERG, PERSIANN-CDR,
  CMORPH, MSWEP, EM-Earth, E-OBS), forecasts (GFS deterministic, GEFS ensemble
  with a `member` dimension), and climate projections (NEX-GDDP-CMIP6,
  NA-CORDEX).
- **Canonical harmonization (`canonical-v1`)**: every connector renames native
  variables to CF-aligned canonical names and converts to canonical SI units
  (`cfs.core.vocabulary`); precipitation and radiation are always returned as
  rates (fluxes), never accumulations.
- **Subsetting**: bounding-box + time-range subsetting for regular lat/lon
  grids (`cfs.subset.bbox`, antimeridian-safe) and 2-D/projected grids
  (`cfs.subset.grid2d` — rotated-pole, Lambert conformal).
- **Reset-aware de-accumulation** (`cfs.subset.deaccumulate`) for running-total
  fields (e.g. ERA5-Land `tp`/`ssrd`/`strd`), plus bucket-aware de-accumulation
  for GEFS 6-hour precipitation/radiation buckets.
- **Derived variables** (`cfs.derive`): specific humidity from 2 m relative
  humidity, temperature, and pressure (Bolton 1980) — used by CARRA, CERRA,
  and GEFS.
- **Range QC** (`cfs.qc`): advisory physical-range checks on every fetch,
  reported in `FetchResult.warnings`, catching unit-conversion errors before
  they reach a model.
- **Fetch guardrails**: bbox-area (`CFS_MAX_AREA_DEG2`) and cell-count
  (`CFS_MAX_CELLS_PER_FETCH`) limits enforced uniformly on the connector base
  class.
- **CLI** (`cfs providers`, `cfs products`, `cfs fetch`) and an in-process
  Python API (`discover()` / `get_connector()` / `fetch()`).
- Protocol mixins for cloud Zarr, OPeNDAP (Earthdata URS), CDS API, HTTP
  byte-range NetCDF/GRIB2, and rclone.

[0.1.0]: https://github.com/DarriEy/CFS/releases/tag/v0.1.0
