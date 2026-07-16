# Provider Catalog

CFS ships **46 connectors** — 43 live-verified against their upstream stores and 3 offline or access-blocked. Counts and the table below are generated from `inventory/providers.yaml`.

The machine-readable catalog (resolution, bbox, license, citation, canonical
variables, caveats) is committed at
[`inventory/providers.yaml`](https://github.com/DarriEy/CFS/blob/main/inventory/providers.yaml),
and the registry is discoverable at runtime (`cfs providers`,
`cfs products`).

!!! note "Live upstreams"
    CFS is a passthrough service: every fetch hits the provider's live store,
    so transient upstream outages (a THREDDS restart, an S3 hiccup, CDS queue
    congestion) can surface as fetch errors independent of CFS itself.

## Connectors

<!-- BEGIN GENERATED PROVIDER TABLE -->
| slug | product | grid | access | auth | verified |
|------|---------|------|--------|------|----------|
| `era5_arco` | ECMWF ERA5 (ARCO / Google Cloud) | regular | zarr | anonymous | live |
| `aorc` | NOAA AORC v1.1 (1 km, hourly) | regular | zarr | anonymous | live |
| `aorc_nwm` | NOAA AORC v1.1 NWM-Projected (1 km, hourly) | lcc_2d | zarr | anonymous | live |
| `chirps` | CHIRPS v2.0 daily precipitation (0.05°) | regular | http | anonymous | live |
| `era5_land` | ECMWF ERA5-Land (0.1°, hourly, via CDS) | regular | cds_api | cds_api | live (creds) |
| `era5_cds` | ECMWF ERA5 (0.25°, hourly, via CDS) | regular | cds_api | cds_api | live (creds) |
| `wfde5` | WFDE5 bias-corrected ERA5 forcing (0.5°, hourly, via CDS) | regular | cds_api | cds_api | live (creds) |
| `carra` | CARRA — Copernicus Arctic Regional Reanalysis (2.5 km, via CDS) | regular | cds_api | cds_api | live (creds) |
| `cerra` | CERRA — Copernicus European Regional Reanalysis (5.5 km, via CDS) | regular | cds_api | cds_api | live (creds) |
| `merra2` | NASA MERRA-2 (0.5°×0.625°, hourly, via OPeNDAP) | regular | opendap | earthdata | live (creds) |
| `nldas` | NLDAS-2 (0.125°, hourly, via OPeNDAP) | regular | opendap | earthdata | live (creds) |
| `gpm` | GPM IMERG Daily precipitation (0.1°, via OPeNDAP) | regular | opendap | earthdata | live (creds) |
| `cmorph` | NOAA CPC CMORPH CDR daily precipitation (0.25°) | regular | http | anonymous | live |
| `gldas` | NASA GLDAS-2 Noah (0.25°, 3-hourly, global land) | regular | opendap | earthdata | live (creds) |
| `daymet` | Daymet V4R1 (1 km daily, North America, via OPeNDAP) | lcc_xy | opendap | earthdata | live (creds) |
| `rdrs` | RDRS / CaSR v3.2 (Canadian Surface Reanalysis, ~10 km, hourly) | rotated_pole_2d | opendap | anonymous | live |
| `conus404` | CONUS404 (4 km WRF reanalysis, hourly) | lcc_2d | zarr | anonymous | live |
| `hrrr` | NOAA HRRR (3 km, hourly) | lcc_2d | zarr | anonymous | live |
| `gridmet` | gridMET daily CONUS surface meteorology (~4 km) | regular | opendap | anonymous | live |
| `nclimgrid_daily` | NOAA nClimGrid-Daily (5 km, CONUS) | regular | opendap | anonymous | live |
| `narr` | NOAA NARR daily monolevel fields (32 km) | lcc_2d | opendap | anonymous | live |
| `barra2` | BoM BARRA-R2 (Australian Regional Reanalysis v2, ~12 km, hourly) | regular | rest | anonymous | live |
| `eobs` | E-OBS European gridded observations (0.1°/0.25° daily, via CDS) | regular | cds_api | cds_api | live (creds) |
| `nex_gddp` | NASA NEX-GDDP-CMIP6 (0.25° daily downscaled CMIP6 projections) | regular | s3_direct | anonymous | live |
| `mswep` | MSWEP (Multi-Source Weighted-Ensemble Precipitation, 0.1°) | regular | rclone | rclone_gdrive | offline |
| `em_earth` | EM-Earth (0.1° daily, global; anonymous FRDR HTTPS / optional S3) | regular | http | anonymous | live |
| `na_cordex` | NA-CORDEX (0.22°/0.44° daily, North America, via S3) | regular | zarr | anonymous | live |
| `nwm_operational` | NWM Operational Forcing (1 km, hourly, via S3) | lcc_2d | s3_direct | anonymous | live |
| `chirts` | CHIRTS daily temperature (0.05°, global tropics, via HTTP) | regular | http | anonymous | live |
| `persiann_cdr` | PERSIANN-CDR daily satellite precipitation (0.25°, via HTTP) | regular | http | anonymous | live |
| `fldas` | NASA FLDAS Noah (0.1° monthly, global/Africa land, via OPeNDAP) | regular | opendap | earthdata | live (creds) |
| `w5e5` | W5E5 v2.0 bias-corrected forcing (0.5°, daily, global, via ISIMIP byte-range) | regular | http | anonymous | live |
| `mrms` | NOAA MRMS MultiSensor QPE (1 km, hourly, CONUS; precip only, via S3 GRIB2) | regular | http | anonymous | live |
| `terraclimate` | TerraClimate global monthly climate & water balance (~4 km, via THREDDS OPeNDAP) | regular | opendap | anonymous | live |
| `livneh` | Livneh CONUS daily gridded meteorology (1/16°, daily, via PSL OPeNDAP) | regular | opendap | anonymous | live |
| `agera5` | AgERA5 agrometeorological indicators (0.1°, daily, global, via CDS) | regular | cds_api | cds | live (creds) |
| `eccc_hrdps` | ECCC HRDPS forecast (2.5 km, continental North America, via MSC Datamart GRIB2) | projected | http | anonymous | live |
| `dwd_icon` | DWD ICON-EU forecast (~6.5 km, Europe, regular lat-lon, via opendata.dwd.de GRIB2) | regular | http | anonymous | live |
| `eccc_geps` | ECCC GEPS ensemble-mean forecast (0.5°, global, via MSC Datamart GRIB2) | regular | http | anonymous | live |
| `eccc_rdps` | ECCC RDPS forecast (10 km, North America, via MSC Datamart GRIB2) | projected | http | anonymous | live |
| `cfsr` | NCEP CFSR selected hourly surface time-series (1979–2010, NCAR GDEX) | regular | opendap | anonymous | live |
| `cfsv2` | NCEP CFSv2/CDAS analysis (~0.5°, hourly, global, via S3 GRIB2) | regular | http | anonymous | offline |
| `gfs` | NOAA GFS atmospheric forecast (0.25°, hourly, global, via S3 GRIB2) | regular | http | anonymous | live |
| `gefs` | NOAA GEFS ensemble forecast (0.25 select, 3-hourly, global, via S3 GRIB2) | regular | http | anonymous | live |
| `rap` | NOAA RAP atmospheric forecast (13 km, hourly, CONUS, via S3 GRIB2) | lcc_2d | http | anonymous | live |
| `nam` | NOAA NAM atmospheric forecast (12 km NA + 3 km CONUS nest, via S3 GRIB2) | lcc_2d | http | anonymous | live |
| `ecmwf_opendata` | ECMWF open-data IFS HRES + AIFS + ENS forecast (0.25°, global, via AWS GRIB2) | regular | http | anonymous | live |
<!-- END GENERATED PROVIDER TABLE -->

"live" means a real fetch against the upstream store returned physical values;
"live (creds)" the same, using real CDS or Earthdata credentials; "offline"
means the connector's path/subsetting/conversion logic is verified against
synthetic data but a live fetch is pending access (see the notes below).

## Per-provider notes

### Rolling-window archives

- **`cmorph`** — the NOAA CPC daily-tar archive only hosts a **rolling recent
  window** (roughly the last couple of months). Historical years are not on
  that endpoint; a fetch outside the window raises a clear "no tar listed"
  error.
- **`nwm_operational`** — reads the real-time NWM forcing from `noaa-nwm-pds`
  (S3), generating lat/lon from the same 1 km LCC projection as `aorc_nwm`.
  Only the `analysis_assim` configuration is exposed (its `tm00` analysis
  maps cleanly to a valid time); the bucket keeps only a **rolling ~4-week
  window**, so fetches must target recent dates.

### Offline-verified connectors

- **`mswep`** — distributed only via a GloH2O-shared Google Drive folder,
  reached through the external `rclone` CLI. The connector's path and
  conversion logic are verified offline (with a clear setup error otherwise);
  a real fetch needs `rclone` plus a configured Drive remote with granted
  access. Exact unblock sequence:

    1. register at <https://www.gloh2o.org/mswep/> (free for non-commercial
       use) with the Google account you will use for Drive;
    2. wait for the GloH2O confirmation email — it shares the `MSWEP_V*`
       Drive folders with that account ("Shared with me");
    3. install rclone and run `rclone config` → new remote, name
       **`GoogleDrive`** (the connector default; override via
       `MSWEP_RCLONE_REMOTE`), type `drive`, default scopes, browser auth
       with the same account;
    4. verify with `rclone lsd --drive-shared-with-me GoogleDrive:` (the
       `MSWEP_V316` folder should list);
    5. run the prepared validation script
       (`/tmp/parity-exp/validate_mswep_when_unblocked.sh`) to exercise the
       doc-fixed paths on both the CFS and SYMFLUENCE sides and record a
       parity grade.
- **`em_earth`** — two sources. The optional S3 source **denies anonymous
  reads** (allows listing only), so it needs AWS credentials
  (`config={"anon": False}`). The **FRDR route** (`config={"source":
  "frdr"}`) needs no credentials at all: FRDR's documented stable per-file
  links (`https://www.frdr-dfdr.ca/repo/files/6/published/publication_542/
  submitted_data/EM_Earth_v1/…`) 302 to the Globus HTTPS collection
  (`g-f0a056.cd4fe.0ec8.data.globus.org`), which serves **anonymous GETs**
  — live-verified 2026-06-12 (an earlier probe of the landing page missed
  this; the dataset page itself only advertises Globus transfer and the
  email-gated Zip). The `EM_Earth_v1/` layout mirrors the S3 keys under
  `nc/` exactly, so the same key construction serves both. Caveats: files
  are whole-month globals (~100–300 MB per variable-month — set
  `data_dir` so they are cached and reused), and the FRDR route covers the
  **deterministic daily** product only (FRDR's probabilistic/hourly trees
  are continent- and member-split).

    Pre-staged files (e.g. bulk Globus transfers from collection
    `515c70c4-2eb8-4f2a-b406-7959b5edc28d`, path
    `/6/published/publication_542/submitted_data`) are picked up from
    `config={"data_dir": ...}` — either the archive-relative layout
    `<data_dir>/deterministic_raw_daily/<var>/<file>.nc` or flat
    `<data_dir>/<file>.nc` — before any network access.

    Units are **file-verified** (2026-06-12, FRDR deterministic daily):
    `prcp` is `mm day-1` (→ `/86400`), temperatures `Celsius` (→ `+273.15`);
    the long-standing "precip units unverified" warning is retired. The
    files also carry `prcp_corrected` (PBCOR WorldClim-corrected); the
    connector ships raw `prcp`, matching the native SYMFLUENCE handler.
    Validation (exp17, Colorado box, 2018-06 ×14 days): CFS canonical
    output is **bitwise identical** to the documented derivations applied
    to the raw FRDR values (`tmean/tdew + 273.15` exactly;
    `prcp × (1/86400)` exactly, ≤ 1 float32 ulp vs the `/86400` op order).
    Native-vs-community parity remains **pending AWS credentials**: the
    native acquirer is S3-only (it grew an `EM_EARTH_S3_ANON: false`
    credentialed path after exp7, but has no FRDR route and no
    local-staging mode for the daily product), and the bucket 403s
    anonymous GETs.

### Forecasts

- **`gfs`** — global 0.25° GFS surface forcing from the `noaa-gfs-bdp-pds` S3
  GRIB2 archive. Reads each file's `.idx` byte-offset index and HTTP
  byte-range fetches only the surface messages it needs (~MB, not the
  ~1.5 GB whole file), decoding with `cfgrib` (the `forecast` extra). For a
  requested range it uses the most recent 00/06/12/18 UTC cycle at/before the
  start; valid times map to lead hours (1-hourly to f120, 3-hourly to f384).
  All fields are identity SI (u/v winds, `prate` flux); radiation and precip
  are interval averages, absent at f000 (analysis). Live-verified (US,
  T 292–300 K, SW 80–228 W m⁻²).
- **`gefs`** — the ensemble companion to `gfs`: same `.idx`/byte-range/cfgrib
  machinery over the GEFS 0.25° select product (`noaa-gefs-pds`), returning a
  `member` dimension (control `gec00` + perturbations `gep01`–`gep30`,
  selected via `config={"members": [...]}`; default all 31). 3-hourly leads
  to f240 (the select product stops there). Instantaneous fields are identity
  SI. Precip (`APCP`) and radiation (`DSWRF`/`DLWRF`) ship as 6-hour-bucket
  quantities (`0-3`, `0-6`, `6-9`, …) — `APCP` accumulates, radiation
  averages — so they are **de-bucketed by lead hour** (value-sign reset
  detection is unsafe when a fresh bucket out-rains the previous one):
  `cur−prev` for accumulations, `2·cur−prev` for averages, then `APCP`
  becomes a flux while radiation stays W m⁻². The select product ships RH,
  not specific humidity, so `q` is **derived** from 2 m RH + temperature +
  pressure (Bolton 1980, `cfs.derive.humidity`); RH is consumed as a
  derivation input, never exposed. Live-verified (non-negative precip, SW ~0
  overnight after de-averaging, derived q 9–17 g/kg, member spread visible).

### Climate projections

- **`nex_gddp`** — NEX-GDDP-CMIP6 downscaled projections. A projection has a
  model × scenario × ensemble axis: the **scenario** is the product ID
  (`nex_gddp:historical`, `nex_gddp:ssp245`, `nex_gddp:ssp585`, …); the
  **model/member** are connector config, e.g.
  `get_connector("nex_gddp")(config={"model": "MPI-ESM1-2-HR", "member": "r1i1p1f1"})`
  (default `ACCESS-CM2` / `r1i1p1f1`). The choices are recorded in
  `FetchResult.provenance` and as dataset attrs
  (`cmip6_model`/`cmip6_scenario`/`cmip6_member`).
- **`na_cordex`** — North-American CORDEX regional projections (S3 Zarr, NCAR
  `ncar-na-cordex`): experiments `eval`/`hist-rcp45`/`hist-rcp85`, with grid
  (`NAM-22i`/`NAM-44i`) and bias-correction (`raw`/`mbcn-Daymet`/
  `mbcn-gridMET`) as `config` knobs; returns the CORDEX multi-model ensemble
  (`member_id` dimension). Relative humidity (`hurs`) is not offered —
  NA-CORDEX lacks the surface pressure needed to derive specific humidity.

### CDS connectors

All need `~/.cdsapirc` and the relevant dataset licences accepted on the CDS
account.

- **`carra` / `cerra`** — delivered interpolated to a **regular grid** via
  the CDS `grid` parameter (the native grids are projected). Both ship 2 m RH
  rather than specific humidity, so `specific_humidity` is derived
  (Bolton 1980). Live-confirmed naming trap: downwelling longwave is
  `thermal_surface_radiation_downwards` on the **CARRA** form but
  `surface_thermal_radiation_downwards` (ERA5-style) on the **CERRA** form —
  and CDS *silently drops* an unknown variable name instead of rejecting the
  request, so getting this wrong yields files with no longwave (caught and
  fixed in the 2026-06-12 parity campaign).
- **`wfde5`** — needs the required CDS `product` token (`wfde5`) and an
  underscore `version` (`2_1`), confirmed against the live form constraints.
  Downloads full half-degree monthly NetCDFs (one CDS request per variable;
  precipitation = `Rainf` + `Snowf`) and subsets locally.
- **`eobs`** — fills the European *observational* gap (CFS otherwise has only
  reanalysis there). Unlike the other CDS connectors, E-OBS has **no
  server-side `area` subset**, so the full European domain is downloaded once
  per variable (large, cached) and subset locally. Exposes only the
  cleanly-convertible fields — `tg`→air_temperature,
  `rr`→precipitation_flux, `qq`→shortwave, `fg`→wind_speed — and **defers**
  `pp` (sea-level, not surface, pressure) and `hu` (relative humidity needs a
  surface pressure E-OBS lacks). Request tokens: `grid_resolution`
  `0_1deg`/`0_25deg`, `version` `31_0e`, `period` `full_period`; version
  override via `config={"version": "30_0e"}`. Live-verified once all E-OBS
  dataset licences were accepted on the CDS account.
- **`era5_cds`** — standard ERA5 single levels via the CDS API (a zip of
  instant + accumulated NetCDFs, merged), as a credentialed alternative to
  the anonymous `era5_arco`.

### Earthdata connectors

All need `EARTHDATA_TOKEN` (or `~/.netrc` / `EARTHDATA_USERNAME` +
`EARTHDATA_PASSWORD`) and the "NASA GESDISC DATA ARCHIVE" app authorized on
the URS profile.

- **`nldas`** — opens one OPeNDAP endpoint **per hour**: fine for short
  windows, slow for long ranges (warned in the `FetchResult`).
- **`gldas`** — same GES DISC `hydro1` host as `nldas`; all GLDAS-2 Noah
  forcing fields are already canonical SI (identity mappings). Two products:
  `gldas:noah025_3h` (GLDAS-2.1, 2000→present) and `gldas:noah025_3h_v20`
  (GLDAS-2.0, 1948–2014). Wind ships as a scalar speed only (no u/v), so it
  maps to `wind_speed`; opens one endpoint per 3-hour stamp (8/day), so long
  ranges are slow (warned in the `FetchResult`).
- **`fldas`** — reuses the GLDAS Earthdata/DAP4 path, all-identity SI, but
  the global product is **monthly** — good for climatology or seasonal
  forcing over Africa and the global land surface, too coarse for
  event-scale hydrology. Wind is a scalar speed; land-only (ocean is fill).
- **`gpm`** — IMERG Daily; adds the Early and Late near-real-time runs
  alongside Final.

### Other notes

- **`barra2`** — uses the anonymous NCI THREDDS **NetcdfSubset** service: the
  server does the bbox+time subset and returns a clean NetCDF, avoiding the
  OPeNDAP DAP2 truncation NCI's server exhibits under concurrent reads. All
  fields are CORDEX/CMIP CF names already in SI (identity mappings, including
  `pr` flux and `huss`); no dewpoint is published. Instantaneous fields are
  stamped on the hour and hourly *means* (`pr`/`rsds`/`rlds`) at the
  half-hour midpoint, so times are floored to the hour to share one axis.
  The grid is regular lat/lon on 0–360 longitudes (requests are normalized).
- **`hrrr`** — adds an `sfc_fcst` product (1-hour forecast) that provides
  precipitation flux, which the analysis product lacks.
- **`aorc_nwm`** — AORC v1.1 on the NWM v3.0 1 km LCC grid (S3 Zarr); lat/lon
  are generated from the LCC projection parameters.
- **`narr`** — includes `dswrf`/`dlwrf` (down short/longwave) radiation,
  live-verified against NOAA PSL. Carries occasional tiny-negative
  precipitation from the source fields (flagged by the advisory range QC).
- **`chirts`** — the temperature companion to `chirps` (UCSB CHC, global
  tropics, 0.05° daily, 1983–2016): `Tmax`/`Tmin` (°C) → `air_temperature` =
  mean + 273.15, read via HTTP byte-range from per-year chunked NetCDFs.
- **`persiann_cdr`** — global daily satellite-precip CDR (NCEI, 0.25°,
  1983→present with multi-week latency). Per-day files carry an unpredictable
  creation-date suffix, so the connector resolves filenames from each year's
  directory index.

## Adding a provider

Want a product that isn't here? Open a
[connector request](https://github.com/DarriEy/CFS/issues/new?template=connector_request.md),
or implement it yourself — see the
[contributing guide](https://github.com/DarriEy/CFS/blob/main/CONTRIBUTING.md)
for the connector pattern.
