# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **RAP forecast connector** (`rap:awp130_fcst`): NOAA Rapid Refresh surface
  forcing — 13 km Lambert Conformal CONUS (grid `awp130`) — from the
  `noaa-rap-pds` GRIB2 archive via the shared Herbie `.idx` byte-range machinery
  (`cfs.connectors.protocols.grib_idx`). Same forecast model as `gfs`/`hrrr`:
  the most recent hourly cycle at/before the start supplies each valid hour
  (`lead = valid − cycle`), to f21 (f51 on the 03/09/15/21Z runs). All fields
  are instantaneous SI (identity): `PRATE` is an instantaneous precipitation
  flux (no de-accumulation), `DSWRF`/`DLWRF` are instantaneous (no
  de-averaging), `SPFH` 2 m is direct (no derivation). Live-probe finding: the
  surface **radiation is absent from the primary `awp130pgrb`** file and lives
  in the secondary `awp130bgrb`, so a piece fetches **two** files per lead
  (pgrb for T/q/dewpoint/pressure/wind/PRATE, bgrb for radiation) and merges
  them. 2-D LCC lat/lon from cfgrib, windowed via `grib_idx.read_field_2d`.
  Needs the `forecast` extra. Live-verified (Colorado 2022-01-01 00z f00–f02:
  multi-lead, in-range T, precip ≥ 0, SW from bgrb ≥ 0).

- **HRRR multi-lead forecast via GRIB2** (`hrrr:sfc_fcst`): the forecast
  product now reads the `noaa-hrrr-bdp-pds` GRIB2 `wrfsfc` archive with the
  Herbie `.idx` byte-range pattern (shared
  `cfs.connectors.protocols.grib_idx`), following the same forecast model as
  `gfs` — the most recent hourly cycle at/before the window start supplies
  every valid hour (`lead = valid − cycle`), out to f18 (f48 on the
  00/06/12/18Z runs). This **replaces** the previous hrrrzarr `step=1`
  implementation, which only ever returned the +1-hour nowcast and could not
  produce a real multi-lead forecast. All exposed fields are instantaneous SI
  (identity): `PRATE` is an instantaneous precipitation flux (no
  de-accumulation), `DSWRF`/`DLWRF` are instantaneous (no de-averaging, unlike
  GFS/GEFS — live-probed), and `SPFH` is shipped directly (no humidity
  derivation). The 2-D LCC lat/lon comes from cfgrib and is windowed with
  `grid2d.subset_2d_grid` via the new `grib_idx.read_field_2d` helper (the
  projected-grid sibling of `read_field`, shared with the planned RAP/NAM
  forecast connectors). The `sfc_anl` analysis product (hrrrzarr) is
  unchanged. Needs the `forecast` extra. Live-verified (Colorado: 2026-06-15
  cycle 11z f00–f04 and 2022-01-01 00z f00–f02 — multi-lead, T 273–291 K,
  q direct, SW 0–646 W m⁻², precip ≥ 0).

- **EM-Earth unblocked via FRDR anonymous HTTPS** (`source: "frdr"` +
  `data_dir` staging on the `em_earth` connector). FRDR's documented stable
  per-file links (`…/repo/files/6/published/publication_542/submitted_data/
  EM_Earth_v1/…`) redirect to a Globus HTTPS collection that serves
  *anonymous* GETs (live-verified 2026-06-12), and the `EM_Earth_v1/` layout
  mirrors the S3 keys under `nc/` exactly — so the connector reuses its key
  construction (deterministic daily only; FRDR's probabilistic tree is
  continent/member-split). `data_dir` doubles as a local-staging mode:
  pre-staged files (archive-relative or flat) are picked up before any
  network access, and FRDR downloads are cached there. The monthly files are
  whole-globe (~100–300 MB each); without `data_dir` they go to a warned
  temp cache. The S3 permission error now points at all three escape
  hatches. Live-validated end-to-end (exp17, Colorado 2018-06): canonical
  output bitwise-identical to the documented derivations applied to the raw
  FRDR values.

### Changed

- **EM-Earth precip units VERIFIED, warning retired**: the FRDR file attrs
  read `prcp: mm day-1` and `tmean`/`tdew: Celsius` — exactly the
  connector's long-standing assumption (`/86400`, `+273.15`). The per-fetch
  "units unverified" warning is removed; the mapping note now records the
  verification. (`EM-EARTH` remains excluded from the parity-gated
  SYMFLUENCE capability table: the *native* acquirer is S3-only —
  credentialed via `EM_EARTH_S3_ANON: false` but with no FRDR route or
  local-staging mode against the 403-gated bucket — so
  native-vs-community parity stays pending AWS credentials.)

- **Four newly parity-gated SYMFLUENCE datasets** (live experiments
  2026-06-12, both sides reading the same upstream archives; campaign
  completed across three sessions with the CDS legs resumed by request ID):
  - `CARRA` → `carra:single_levels` (exp11: `value-identical:grib-repack` —
    time bitwise; every field differs only by CDS/MARS re-encoding the GRIB
    per request — the native handler pads the area ±0.1°, so the 16-bit
    packing anchors differ (verified quantization combs: T on 2⁻¹⁵ K, p on
    2⁻⁷ Pa, offset anchors); plus the documented specific-humidity ε
    derivation, ≤ 5.5e-5 rel). Arctic-only spatial domain; the native
    `CARRA_DOMAIN` key drives the CDS west/east domain split.
  - `CERRA` → `cerra:single_levels` (exp12: same `grib-repack` grade;
    pressure + time bitwise). Europe-only; archive ends 2021-06-30. CERRA
    publishes a combined 10 m wind speed, so no u/v components are claimed.
  - `HRRR` → `hrrr:sfc_anl` (exp14: `bit-identical` — all 7 analysis
    variables + time bitwise on the overlaid window; 2-D lat/lon differ
    ≤ 3.9e-6° because the native handler *recomputes* them with pyproj while
    CFS reads the archive's published grid arrays). Hourly 3 km LCC from the
    public hrrrzarr S3 archive; the analysis stream has no precipitation on
    either side. Native-side finding from the campaign: the native bbox
    windowing no-ops (the hrrrzarr variable groups carry no latitude
    coordinate to mask on), so the native handler downloads the **full CONUS
    grid** (~1.3 GB/day, 42 min for the 1-day experiment) where the community
    fetch windows to the bbox (96 s). CONUS-only; legacy `HRRR_VARS` key
    translated.
  - `DAYMET` → `daymet:daily_v4` (exp16: `bit-identical` — **full-grid**:
    all four canonical derivations (`T=(tmax+tmin)/2+273.15`, `prcp/86400`,
    `srad·dayl/86400`, inverse-Bolton dewpoint) recomputed in float32 from the
    raw Daymet granule values are bitwise identical to the community artifact
    across all 57 × 46 × 14 = 36 708 cells per variable, and the 2-D lat/lon
    grid and time axis are bitwise identical too. The raw window was fetched
    independently over the same Hyrax DAP2 hyperslab route (curl + EDL cookies,
    after the staged pydap session hit intermittent 120-s read timeouts);
    only the HTTP client differs. The native-op-order shortwave variant
    `srad·(dayl/86400)` is the lone non-bitwise case (≤ 2 f32 ulps, op-order
    only). The earlier point-sampled run (5 cells via ORNL's independent
    single-pixel API, with exact containing-cell LCC identity) stands as
    corroboration. Native-side findings (the parity verdict uses an independent
    raw route, not the native gridded path): the native gridded OPeNDAP route
    slices the descending Daymet y axis with an ascending slice and returns
    empty subsets (a repair exists on `fix/native-acquisition-bugs`, not yet
    merged to develop), and there is no THREDDS-NCSS alternative in the handler.
    Daily 1 km LCC, North-America-only; legacy `DAYMET_VARIABLES` key
    translated.
- **Spatial domains on capabilities** (`DatasetSpec.spatial`, minimal honest
  extension): regional datasets refuse out-of-domain bboxes at `acquire()`
  time with a plain `AcquisitionError` — deliberately not a
  decline-and-fallback, because the limit is a property of the dataset (no
  backend can serve CARRA south of 55°N). Moves to selection time if/when the
  contract's `DatasetCapability` grows a spatial field.

- **Three newly parity-gated SYMFLUENCE datasets** (live experiments
  2026-06-12, both sides reading the same upstream archives):
  - `CONUS404` → `conus404:hourly` (HyTEST OSN Zarr; exp13:
    `value-identical:1ulp` — T/q/p/u/v + derived wind_speed bitwise, precip
    and radiation ≤ 1 float32 ulp from `/3600` vs `*(1/3600)` op order; the
    first radiation step differs by design — native back-fills it from
    step 2, the community pipeline de-accumulates against a real pre-window
    hour). Declares WY1980–WY2022 coverage (`WindowOutOfRange` outside it).
  - `NWM3_RETROSPECTIVE` → `aorc_nwm:conus_1km` (NWM v3.0 retrospective
    forcing Zarr; exp15: `bit-identical` — all 8 variables + 2-D lat/lon +
    time bitwise; canonical precipitation is a flux while the native file
    ships the identical values ×3600 as hourly accumulation,
    value-equivalent). Serves the `forcing` output type only; declares
    1979-02 – 2023-01 coverage. Legacy `NWM3_VARIABLES` key translated.
  - `CASR` → `rdrs:casr_v32` as an **alias capability** (no new connector):
    the CASR identity investigation concluded SYMFLUENCE's CASR is the same
    ECCC CaSR product family as RDRS — natively it is MAF/datatool-only
    (HPC-prestaged CaSR **v3.1**, RPN names like `CaSR_v3.1_P_TT_1.5m`),
    the PAVICS THREDDS catalog publishes **only CaSR v3.2** (hourly + daily
    NCML; no v3.1/v2.x variants), and that v3.2 store is exactly what the
    `rdrs` connector reads (bitwise vs the native `RDRSAcquirer`, exp10;
    `casr_utils` explicitly consumes that consolidated layout too). The
    capability notes document that the community backend serves v3.2.
- **`CanonicalV1Handler` projected-grid generalization**: the monthly-split
  merge now rebuilds each month's time axis at the store's *native step*
  (median time diff) instead of a hard-coded hourly axis — hourly stores
  (RDRS/CONUS404/HRRR/NWM3) keep the exact native full-month behaviour,
  daily stores (Daymet) stay daily and keep their noon anchoring, and gaps
  are filled at the native step. Hermetic grid-family tests added for the
  LCC `y`/`x` layout (process/merge/shapefile) and the daily-LCC Daymet
  layout (no hourly inflation; gap restore at the daily step).

### Fixed

- **CERRA longwave silently missing** (`connectors/cerra.py`): the connector
  requested the CARRA-style CDS variable name
  `thermal_surface_radiation_downwards`; the CERRA form only knows the
  ERA5-style `surface_thermal_radiation_downwards`, and CDS **silently
  drops** unknown names instead of rejecting the request, so CERRA fetches
  delivered no `surface_downwelling_longwave_flux` (and no warning). Caught
  by the exp12 parity run — the same bug exists in the native SYMFLUENCE
  CERRA handler, where the required-variable validation then hard-fails, so
  native CERRA acquisition cannot complete at all. Fixed name live-verified.

### Changed

- **SYMFLUENCE protocol target bumped to 0.2.0**: the framework's contract
  bump added the observation flavour (`ObservationBackend` et al.) and left
  the `AcquisitionBackend` surface untouched (verified against the contract
  diff); `TARGET_INTERFACE_VERSION` re-targeted so the backend registers
  again under the pre-1.0 minor-is-breaking rule.

### Documented

- **EM-Earth / FRDR access mechanics** (probed 2026-06-12): the FRDR archive
  (DOI 10.20383/102.0547) offers only a Globus transfer (collection
  `515c70c4-2eb8-4f2a-b406-7959b5edc28d`) and an email-gated whole-dataset
  zip — **no per-file HTTPS**, so no `frdr` source option was added to the
  `em_earth` connector; the Globus staging steps are documented in the
  catalog notes.
- **MSWEP unblock sequence** (GloH2O registration → Drive share → rclone
  `GoogleDrive` remote) documented step-by-step in the catalog notes, with a
  ready-to-run two-sided validation script staged at
  `/tmp/parity-exp/validate_mswep_when_unblocked.sh`.

## [0.3.0] — 2026-06-11

### Added

- **SYMFLUENCE drop-in backend via entry-point plugin**
  (`cfs.integrations.symfluence`): installing CFS next to SYMFLUENCE makes
  CFS a drop-in replacement for SYMFLUENCE's in-tree forcing acquisition —
  users keep their existing configs (`FORCING_DATASET: ERA5`, …) and enable
  it with the existing `DATA_ACCESS` gate's new `community` value. At
  registration (which runs after SYMFLUENCE's in-tree handlers register),
  the plugin captures each native acquisition class and re-registers a
  **shadow wrapper under the same name**; the wrapper routes per
  `download()` call: `DATA_ACCESS: community` → `cfs.fetch_sync` writing
  canonical-v1 NetCDF (the `cfs_schema` attr is the downstream detection
  key), anything else → the captured native class runs unchanged
  (bit-identical default). Per-dataset opt-out via flat
  `<NATIVE_NAME>_BACKEND: native` keys. The shadow map is **parity-gated**
  (live-measured 2026-06-11, both sides reading the same upstream archives):
  ERA5→`era5_arco:single_levels` (≤ 2 float32 ulps on the 3
  accumulation→flux vars, op-order only; the SYMFLUENCE-derived
  `wind_speed`/`specific_humidity` are recomputed with the native op order,
  bitwise equal), NLDAS/NLDAS2/NLDAS-2→`nldas:fora0125_h` (bitwise 7/8 vars,
  precip ≤ 1 ulp), AORC→`aorc:conus_1km` (bit-identical),
  NEX-GDDP(-CMIP6)→`nex_gddp:<scenario>` built from the native `NEX_*`
  config keys (bit-identical; same synthetic surface pressure as the native
  handler). MSWEP and EM-EARTH are deliberately NOT shadowed until live
  parity validation is possible (blocked: rclone Drive remote / S3
  credentials); projected-grid datasets are not shadowed in v1. Matching
  **self-detecting dataset-handler shadows** (`era5`, `aorc`, `nex-gddp`,
  `nex-gddp-cmip6`) route canonical-v1 raw files to the canonical→CFIF path
  and delegate native-format files to the captured native handler — per-file
  in `process_dataset`, so mixed raw directories work; `nldas`/`nldas2`/
  `nldas-2` (which have no native dataset handler in SYMFLUENCE) are
  registered outright for community-acquired canonical files only.
- **Parallel-name mode**: every CFS product is also available as a
  SYMFLUENCE forcing dataset (`FORCING_DATASET: CFS` +
  `CFS_PRODUCT: <provider:product>`), with no manual registration —
  SYMFLUENCE discovers the `symfluence.plugins` entry point on import.
  Ships `CFSForcingAcquirer` (acquisition handler wrapping `cfs.fetch_sync`,
  registered as `'CFS'`) and `CFSDatasetHandler` (CFIF preprocessing
  handler, registered as `'cfs'`; canonical-v1 names map to CFIF by
  identity). Regular latitude/longitude grids only in v1; projected-grid
  products raise `NotImplementedError`. SYMFLUENCE base classes are imported
  defensively, so `import cfs` never requires (or fails without) SYMFLUENCE,
  and SYMFLUENCE is **not** a dependency. Documented in `docs/symfluence.md`.

### Fixed

- **MSWEP Drive paths corrected to the documented GloH2O V3.x layout**
  (`{VERSION}/{Past|Past_nogauge|NRT}/{3hourly|Daily}/YYYYDOY[.HH].nc`):
  the connector previously planned paths with a spurious per-year subfolder,
  no product level, and filenames missing the year and hour separator
  (e.g. `MSWEP_V300/3hourly/2023/15200.nc` instead of
  `MSWEP_V316/Past/3hourly/2023152.00.nc`). Default version is now `V316`
  (folder `MSWEP_V316`) and the product level (`Past`/`Past_nogauge`/`NRT`)
  is a new config knob defaulting to `Past` (the Past→NRT cutover is a
  moving GloH2O boundary, not statically derivable from the date).
  Doc-validated against the official worked example
  (`MSWEP_V315/Past/Hourly/2020116.18.nc`) and offline-tested; still
  `verified: false` pending a live authenticated rclone run. Caveat: GloH2O
  flags a 2000–2015 low-precipitation artifact in V3.15/V3.16.
- **NEX-GDDP-CMIP6 file-version selection made explicit**: the per-year file
  resolver relied on `"_v" in name` with last-match-wins over the S3 listing
  order, which only preferred `_v2.0` because it happens to sort after
  `_v1.1`. It now parses the `_vN[.N…]` suffix into a numeric version tuple
  (unsuffixed = version 0) and picks the highest, so `_v10.0` beats `_v2.0`
  regardless of listing order.
- **RDRS/CaSR fetch no longer pulls the full 45-year time axis**: the
  connector opened the PAVICS NCML aggregation with `chunks={}`
  (whole-variable dask chunks), so dask fused the rlat/rlon window into the
  OPeNDAP constraint but not the time slice — every variable read pulled
  1980–2024 for the spatial window (~128 MB/var), once for the QC sample
  inside `fetch` and again on the caller's `.load()` (~2.3 GB / ~30 min
  for a 3-day, 9-variable request). The store is now opened without dask
  (plain lazy backend indexing, like the other anonymous OPeNDAP
  connectors), so time and space both push down into one small DAP
  hyperslab per variable, and the connector materializes the harmonized
  subset exactly once before QC runs (its `lazy=False` flag is now true).
  A 3-day Logan-box fetch moves ~0.5 MB of data payload instead of
  ~2.3 GB and is live-verified bitwise-identical to the native SYMFLUENCE
  RDRS handler on all 9 mapped variables. No other connector mixes dask
  single-chunk opens with OPeNDAP (gridMET/NARR/nClimGrid-Daily and the
  Earthdata mixin already open pydap-lazy without dask).
- **NLDAS-2 fetch batched to one request per hour-file**: each hour was
  opened lazily via pydap and pulled per variable (~10 HTTP round-trips per
  hour-file, projected >3 h for a 49-hour window). The connector now issues
  a single combined OPeNDAP constraint request per hour
  (`…nc4?Tair[0][lat][lon],…,time[0],lat[…],lon[…]` — all requested
  variables plus coordinates, server-side cropped via hyperslab indices on
  the fixed 0.125° grid, trimmed locally to the exact bbox), concurrent up
  to `CFS_FETCH_CONCURRENCY`. Live-verified against GES DISC.

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
