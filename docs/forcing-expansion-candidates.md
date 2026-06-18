# Forcing dataset expansion candidates

Verified license posture and concrete access endpoints for forcing datasets
proposed as new CFS connectors. Researched and adversarially fact-checked
**2026-06-18** against primary provider sources (live HTTP 200 checks where
noted). License classes mirror the `_FORCING_POSTURE` model in
`src/cfs/integrations/symfluence.py`: `open` (CC0 / public-domain, no
attribution), `attribution` (CC-BY-4.0 or equivalent requiring credit),
`restricted` (non-commercial / no-redistribution — declared but not re-served).

This file is the standing evidence for the connectors added under "forcing
expansion — waves 1–3". Re-verify a row's license/endpoint before relying on it
for anything legally load-bearing; provider terms and cloud buckets drift.

## Build status

| Dataset | Wave | License | Access | Status |
|---|---|---|---|---|
| CFSv2 / CDAS | 1 | open (US-PD) | anonymous S3 + `.idx` | connector `cfsv2` |
| DWD ICON (global + EU) | 1 | attribution (CC-BY-4.0) | anonymous HTTP GRIB2 | connector `dwd_icon` |
| ECCC HRDPS / RDPS / GEPS | 1 | attribution (ECCC v2.1) | anonymous HTTPS GRIB2 | connectors `eccc_hrdps` / `eccc_rdps` / `eccc_geps` |
| W5E5 v2.0 | 2 | open (CC0) | anonymous ISIMIP REST | connector `w5e5` |
| AgERA5 | 2 | attribution (CC-BY-4.0) | CDS auth | connector `agera5` |
| TerraClimate | 3 | open (CC0) | anonymous THREDDS/OPeNDAP | connector `terraclimate` |
| Livneh | 3 | open (US-PD) | anonymous PSL OPeNDAP | connector `livneh` (4/8 vars) |
| MRMS QPE (+ Stage IV) | 3 | open (US-PD) | anonymous S3 (`.grib2.gz`) | connector `mrms` (precip-only) |
| CFSR | 2 | attribution (US-PD / CC-BY via RDA) | NCAR RDA | reanalysis tier (deferred) |

## Live forecasting tier (priority)

### 1. DWD ICON (global + ICON-EU) — easiest live forecast

- **License:** CC-BY-4.0 (GeoNutzV / DWD open data). Redistribution of
  regridded/re-served data permitted with source acknowledgement; a "data
  modified" note is customary for regridded output. Aviation-certified products
  are access-restricted — not relevant to the openly-served NWP forcing.
  Attribution: *"Deutscher Wetterdienst (DWD)"*.
- **Access (anonymous plain HTTP, no key/login):**
  `https://opendata.dwd.de/weather/nwp/icon/grib/{00,06,12,18}/` (global),
  `…/icon-eu/grib/{HH}/` (EU nest). Apache directory index, GRIB2 (often
  `.grib2.bz2`). Cloud-native Zarr mirror also exists:
  `s3://dynamical-dwd-icon-eu` (us-west-2, anonymous, CC-BY-4.0).
- **Data:** all 8 forcing vars. Global ICON ~13 km, ICON-EU ~6.5 km, GRIB2.
- **Forecast:** 4 cycles/day (00/06/12/18); ICON global to 180 h at 00/12, 120 h
  at 06/18; ICON-EU to 120 h. Files appear within a few hours of cycle time;
  retention window on the open server is short (near-real-time download).

### 2. ECCC HRDPS / RDPS / GEPS — Canadian operational suite

- **License:** Environment and Climate Change Canada Data Servers End-use
  Licence **v2.1** (Sept 2022). Attribution-style; commercial use, derivatives
  and redistribution permitted, conditional on attribution (failure to attribute
  auto-terminates the grant). **Same posture string already used for RDRS/CASR.**
  Attribution: *"Data Source: Environment and Climate Change Canada"*.
- **Access (anonymous HTTPS, no auth):** MSC Datamart, dated path
  `https://dd.weather.gc.ca/{YYYYMMDD}/WXO-DD/...` (the README `today/` prefix is
  an alias; bare `/model_*/` now 404s). One field+level per GRIB2 file (RDPS,
  HRDPS); one field+level × all 21 members per file for GEPS raw.
  - **HRDPS** (2.5 km continental): `…/model_hrdps/continental/2.5km/{HH}/{hhh}/`,
    cycles 00/06/12/18Z, to 48 h, hourly.
  - **RDPS** (10 km): `…/model_rdps/10km/{HH}/{hhh}/`, cycles 00/06/12/18Z,
    to 84 h, hourly.
  - **GEPS** (0.5° ensemble, 21 members): `…/ensemble/geps/grib2/raw/{HH}/{hhh}/`,
    cycles 00/12Z, to 384 h (3-hourly to 192 h, then 6-hourly); extended to 936 h
    on the 00Z run Mon/Thu.
- **Data:** all 8 forcing vars in all three; wind as u/v. Radiation/precip are
  **time-accumulated** (`-Accum` / `APCP`) → must de-accumulate.
- **⚠️ Variable naming differs per model — a connector must map per-model:**
  - **RDPS** = new MSC camelCase: `AirTemp_AGL-2m`, `SpecificHumidity_AGL-2m`,
    `Pressure_Sfc`, `WindU_AGL-10m`/`WindV_AGL-10m`,
    `DownwardShortwaveRadiationFlux-Accum_Sfc`,
    `DownwardLongwaveRadiationFlux-Accum_Sfc`, `Precip-Accum_Sfc`.
  - **HRDPS** = old NCEP short names: `TMP_AGL-2m`, `SPFH_AGL-2m`, `PRES_Sfc`,
    `UGRD_AGL-10m`/`VGRD_AGL-10m`, `DSWRF_Sfc`, `DLWRF_Sfc`, `APCP_Sfc`.
  - **GEPS** = NCEP short names + numeric levels + ensemble files:
    `CMC_geps-raw_TMP_TGL_2m_..._allmbrs.grib2`, `UGRD_TGL_10m`, `DSWRF_SFC_0`,
    `DLWRF_SFC_0`, `APCP_SFC_0`, `SPFH_TGL_2` (note `2`, not `2m`).
  - GEPS **does** carry `DSWRF`/`DLWRF` — README summaries that omit them are
    wrong (verified present in live listing 2026-06-18).

### 3. CFSv2 / CDAS — global analysis stream

- **License:** US public domain via NOAA NODD on AWS/NCEI (redistribution OK,
  attribution requested). The *same* data at NCAR RDA/GDEX is wrapped under
  CC-BY-4.0 — cite per the channel you fetch from.
- **Access (anonymous S3, `--no-sign-request`):** `s3://noaa-cfs-pds`
  (`https://noaa-cfs-pds.s3.amazonaws.com`, us-east-1). Layout:
  `cdas.YYYYMMDD/cdas1.tHHz.sfluxgrbf{00..09}.grib2` (+ `.grib2.idx` sidecar).
  Cycles 00/06/12/18Z. Surface-flux file = `sfluxgrbf`.
- **Data:** all 8 forcing vars in `sfluxgrbf`, **NCEP/GFS-family short names**
  (`TMP`/`SPFH`/`PRES`/`UGRD`/`VGRD`/`PRATE`/`DSWRF`/`DLWRF`) → identity-SI maps,
  reuses the `gfs.py`/`gefs.py` `grib_idx` byte-range protocol. Global Gaussian
  T574 (~0.5°). Wind u/v.
- **⚠️ CRITICAL:** the AWS `noaa-cfs-pds` bucket is the **CDAS real-time analysis
  + short-range (f00–f09) stream — NOT the 9-month seasonal forecast** (despite
  the registry's "Operational Forecasts" label). It is the analysis backbone
  that extends CFSR. Instantaneous state vars (`TMP`/`SPFH`/`PRES`/`UGRD`/`VGRD`)
  carry `anl`; `PRATE`/`DSWRF`/`DLWRF` are interval averages on the fNN leads. For
  true seasonal leads use NOMADS
  `https://nomads.ncep.noaa.gov/pub/data/nccf/com/cfs/prod/` (7-day rolling) or
  NCAR RDA `ds094.0`. AWS bucket history begins 2023-04-22.

### MRMS QPE (+ Stage IV) — real-time precip, nowcasting

- **License:** US public domain (NOAA NODD). Stage IV via NCAR RDA labels
  CC-BY-4.0; underlying NCEP/EMC product is US-PD.
- **Access (anonymous S3, `--no-sign-request`):** `s3://noaa-mrms-pds`
  (us-east-1). CONUS key:
  `CONUS/<PRODUCT>/<YYYYMMDD>/MRMS_<PRODUCT>_<YYYYMMDD>-<HHMMSS>.grib2.gz`.
  Products: `MultiSensor_QPE_{01H,03H,…,72H}_{Pass1,Pass2}_00.00`,
  `RadarOnly_QPE_{15M,01H,…}_00.00`. **Files are gzip-compressed GRIB2
  (`.grib2.gz`) — must gunzip before cfgrib.** Stage IV real-time via NWPS HTTP
  `https://water.noaa.gov/resources/downloads/precip/stageIV/`.
- **Data:** **precipitation ONLY** — cannot serve the full 8-var set. MRMS ~1 km
  (0.01°), 2-minute cadence (MultiSensor Pass1 ~1 h gauge latency, Pass2 ~2 h,
  RadarOnly lowest). MRMS S3 retention ~5.6 yr rolling (earliest 2020-10-14), not
  a full archive. Stage IV 4 km HRAP polar-stereographic, 2002→present.
- **Pair with a full-forcing source** (HRRR/GFS/AORC/NLDAS) for complete forcing.

## Historical / reanalysis tier

### W5E5 v2.0 — bias-corrected global forcing

- **License:** **CC0-1.0** (the *data*; the isimip.org website text is CC-BY — a
  CC-BY claim on the data was refuted 0-3). No attribution legally required;
  ISIMIP socially requests the DOI citation `10.48364/ISIMIP.342217`.
- **Access (anonymous, verified live):** ISIMIP repo `data.isimip.org`. Dataset
  id `a5f9441d-92e4-42ca-bb57-c9997c9a89b4`. Machine-readable APIs:
  `/api/v1/datasets/{id}/filelist/` (plain-text direct URLs),
  `/api/v1/datasets/{id}/manifest/`, per-file `/files/{file_id}/`; downloads from
  `files.isimip.org` need no auth.
- **Data:** all 8 forcing vars (`tas`, `pr`, `huss`, `ps`, `rsds`, `rlds`, +
  wind). Global 0.5°, daily, 1979–2019, NetCDF. **⚠️ Wind is scalar `sfcWind`,
  not u/v** — map to `wind_speed` (like NEX-GDDP / gridMET).

### AgERA5 — agrometeorological indicators

- **License:** CC-BY-4.0 (since the 2 Jul 2025 Copernicus migration). Attribution
  to *"Copernicus Climate Change Service (C3S)"*.
- **Access (auth-gated):** CDS, dataset id `sis-agrometeorological-indicators`,
  DOI `10.24381/cds.6c68c9bb`. Needs a CDS account + personal access token —
  reuses CFS's `CDSAPIMixin`. See the CDS breaking change below.
- **Data:** all 8 forcing vars (daily agromet indicators). Global 0.1°, daily,
  1979→present, NetCDF-4. Reanalysis-derived (not a forecast).

### CFSR — historical reanalysis (deferred connector)

- **License:** US-PD via NCEI; CC-BY-4.0 via NCAR RDA (cite per channel).
- **Access:** NCAR RDA `ds093.0` (`https://gdex.ucar.edu/datasets/d093000/`),
  1979-01 → 2011-03; extended forward by CFSv2/CDAS. Native Gaussian T382
  (~0.3°) plus 0.5°/1.0°/2.5° regridded.
- **Data:** all 8 forcing vars, u/v wind, 6-hourly (hourly time-series at
  RDA/NCEI). Wire as the historical companion to the CFSv2/CDAS connector.

### TerraClimate — global monthly water balance

- **License:** **CC0-1.0** (Climate Data Guide lists "Usage Restrictions: None").
  Citation requested as good practice: Abatzoglou et al. 2018, *Sci. Data*
  5:170191.
- **Access (anonymous THREDDS/OPeNDAP, verified live):**
  `http://thredds.northwestknowledge.net:8080/thredds/dodsC/agg_terraclimate_<VAR>_1950_CurrentYear_GLOBE.nc`.
  **⚠️ Filename quirk:** `srad` and `swe` use a hyphen (`…_1950-CurrentYear_…`);
  all others use underscore. Per-year files under
  `…/TERRACLIMATE_ALL/data/`.
- **Data:** all 8 forcing-relevant vars (`tmax`, `tmin`, `ppt`, `srad`, `vap`,
  `ws` scalar wind, `soil`, `pet`). Global 1/24° (~4 km), **monthly only**,
  1950→present, NetCDF4. `tmax`/`tmin`/`ppt` stored Int16/Int32 with
  scale/offset — readers must apply them.
- **⚠️ Monthly resolution only** → climatological/water-balance forcing, not
  sub-daily/daily hydrologic forcing. Flag as low-temporal-resolution.
- **⚠️ Recent change (live metadata 2026-06-02):** now V1.1, temporal start
  re-based to **1950** (not the 1958 of the 2018 paper), ERA5-anomaly method.
  Trust the live netCDF metadata over the climatologylab.org page.

### Livneh — CONUS daily gridded meteorology (partial)

- **License:** US public domain (NOAA / 17 U.S.C. 105) with PSL attribution
  request. Cite: Livneh et al. (2015), *Sci. Data* 2:150042.
- **Access (anonymous PSL OPeNDAP — matches `nclimgrid_daily.py`):**
  `https://psl.noaa.gov/thredds/dodsC/Datasets/livneh/metvars/{var}.{YYYY}.nc`,
  vars `prec`, `tmax`, `tmin`, `wind`. THREDDS catalog:
  `https://psl.noaa.gov/thredds/catalog/Datasets/livneh/metvars/catalog.html`.
- **Data:** **only 4/8 forcing vars** — `prec` (mm/day), `tmax`/`tmin` (°C),
  `wind` (scalar, m/s). **No radiation, humidity, or pressure** (same limitation
  as Daymet/nClimGrid). 1/16° (~6 km), CONUS + S. Canada (Mexico in the full
  product), daily, one file per year per var, NetCDF-3/CF-1.2.
- **⚠️ Gotchas:** var token is `prec` (not `precip`); only tmax/tmin (no tavg);
  time epoch is `days since 1915-1-1` even for the 1950-onward record. PSL data
  pages are JS-rendered — use the THREDDS/OPeNDAP DDS forms for ground truth.

## Breaking changes that touch existing CFS code

1. **CDS decommissioned 26 Sep 2024.** Legacy CDS/ADS replaced by the Common
   Data Store Engine. Old credentials fail; the new `~/.cdsapirc` has **no UID
   field** (token is `<UID>:<APIKEY>` in `key`). Affects `era5_cds`,
   `era5_land`, `wfde5`, `carra`, `cerra`, `eobs`, and the new `agera5`. The
   `CDSAPIMixin` error text already points at the new `how-to-api` URL; verify
   existing user `.cdsapirc` files are migrated.
2. **Copernicus → CC-BY-4.0 (2 Jul 2025).** Confirms the existing
   ERA5/CARRA/CERRA/WFDE5 posture metadata; AgERA5 inherits it.
3. **ECCC Datamart moved to dated paths** `dd.weather.gc.ca/{YYYYMMDD}/WXO-DD/…`
   — bare `/model_*/` now 404s; `today/` is an alias.
4. **NCAR RDA → `gdex.ucar.edu`** (rda.ucar.edu 301-redirects).
5. **TerraClimate re-based to V1.1** (1950 start, ERA5-anomaly method) as of
   2026-06-02.

## Excluded — restricted posture

Declared `restricted` in `_FORCING_POSTURE` (or simply not added) and **not**
re-served through the community backend, consistent with E-OBS / MSWEP /
NA-CORDEX:

- **PRISM** (US) — restrictive redistribution terms.
- **MSWX** — CC-BY-NC (the temperature/forcing sibling to the already-restricted
  MSWEP).
