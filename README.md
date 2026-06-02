# CFS — Community Forcing Service

Acquire-and-subset access to meteorological **forcing** products for hydrological
modelling. The third member of the community-data triad alongside
**CAS** (Community Attribute Service) and **CSFS** (Community Streamflow Service):

| Service | Data | Returns |
|---------|------|---------|
| CAS  | geospatial attributes (DEM, soil, land cover) | harmonized zonal statistics |
| CSFS | streamflow observations | harmonized station time series |
| **CFS** | **meteorological forcing** | **canonical, subset `xarray.Dataset`** |

## The boundary (why CFS stops where it does)

CFS does exactly one job: **acquire a forcing product, subset it to a bounding
box + time range, harmonize it to a canonical schema, and hand back a lazy
`xarray.Dataset`.** That's it.

It deliberately does **not**:

- remap to HRUs / sub-basins,
- write model-specific forcing schemas (SUMMA, FUSE, mizuRoute, …),
- serialize monthly NetCDF chunks or handle HPC filesystem locking.

Those steps are model- and deployment-specific, so they stay in the consumer
(e.g. SYMFLUENCE). Keeping the boundary here is what makes CFS reusable across
frameworks rather than a SYMFLUENCE library in disguise.

```
 upstream store ──▶  subset to bbox+time  ──▶  harmonize to canonical  ──▶  xr.Dataset
   (Zarr/S3/…)        cfs.subset.bbox            cfs.subset.canonical          │
                                                                               ▼
                                              [ consumer: HRU remap + model schema ]
```

## Canonical schema (`canonical-v1`)

Every connector renames native variables to CF-aligned canonical names and
converts to canonical SI units (see `cfs/core/vocabulary.py`). Precipitation and
radiation are always returned as **rates** (`kg m-2 s-1`, `W m-2`), never
accumulations — the conversion that most often goes wrong is done once, here.

## Install

```bash
pip install -e '.[climate]'      # xarray, zarr, gcsfs, dask, netcdf4
```

## Use

```bash
cfs providers                    # list registered providers
cfs products                     # list products + canonical variables
cfs fetch \
  -P era5_arco:single_levels \
  -b -114.5,50.7,-114.0,51.1 \
  --start 2015-06-01T00:00 --end 2015-06-01T06:00 \
  -v air_temperature,precipitation_flux
```

Python:

```python
from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar

discover()
Conn = get_connector("era5_arco")
async with Conn() as conn:
    ds, result = await conn.fetch(
        "era5_arco:single_levels",
        BoundingBox(min_lon=-114.5, min_lat=50.7, max_lon=-114.0, max_lat=51.1),
        TimeRange(start=..., end=...),
        variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
    )
# ds: lazy canonical cube;  result: FetchResult provenance/shape metadata
```

## Adding a connector

Subclass `BaseForcingConnector` (optionally mix in `ZarrStoreMixin`), implement
`list_products()` and `fetch()`, declare a `VariableMapping` table mapping native
names → canonical vars + linear unit conversions, and decorate with
`@register("slug")`. `discover()` finds it automatically.

## Providers

Implemented — 33 connectors (31 live-verified: 19 anonymous + 12 auth-gated
confirmed with real CDS + Earthdata credentials; 2 offline-verified pending live
access or provider-specific credentials):

| slug | product | grid | access | verified |
|------|---------|------|--------|----------|
| `era5_arco` | ECMWF ERA5 (0.25°, hourly) | regular | GCS Zarr | live |
| `aorc` | NOAA AORC v1.1 (1 km, hourly) | regular | S3 Zarr | live |
| `aorc_nwm` | NOAA AORC v1.1 NWM-Projected (1 km) | 2-D LCC | S3 Zarr | live |
| `nwm_operational` | NOAA NWM operational forcing (1 km, hourly, real-time) | 2-D LCC | S3 NetCDF | live∇ |
| `gfs` | NOAA GFS atmospheric **forecast** (0.25°, hourly, global) | regular | S3 GRIB2 (byte-range) | live⊗ |
| `gefs` | NOAA GEFS **ensemble forecast** (0.25°, 3-hourly, global) | regular | S3 GRIB2 (byte-range) | live⊠ |
| `chirps` | CHIRPS v2.0 daily precip (0.05°) | regular | HTTP NetCDF | live |
| `chirts` | CHIRTS daily temperature (0.05°, global tropics) | regular | HTTP NetCDF | live |
| `persiann_cdr` | PERSIANN-CDR daily satellite precip (0.25°, 1983–) | regular | HTTP NetCDF | live |
| `rdrs` | RDRS / CaSR v3.2 (Canada, ~10 km, hourly) | 2-D rotated pole | OPeNDAP | live |
| `barra2` | BoM BARRA-R2 (Australia, ~12 km, hourly) | regular | NCI THREDDS ncss | live◊ |
| `conus404` | CONUS404 (4 km WRF, hourly) | 2-D LCC | OSN Zarr | live |
| `hrrr` | NOAA HRRR analysis + forecast (3 km) | 2-D LCC | hrrrzarr S3 | live |
| `era5_land` | ECMWF ERA5-Land (0.1°, hourly) | regular | CDS API | live (creds) |
| `era5_cds` | ECMWF ERA5 reanalysis (0.25°, hourly) | regular | CDS API | live (creds) |
| `wfde5` | WFDE5 bias-corrected ERA5 forcing (0.5°, hourly) | regular | CDS API | live (creds)✦ |
| `carra` | Copernicus Arctic Regional Reanalysis (2.5 km) | regular† | CDS API | live (creds) |
| `cerra` | Copernicus European Regional Reanalysis (5.5 km) | regular† | CDS API | live (creds) |
| `eobs` | E-OBS European gridded **observations** (0.1°/0.25° daily) | regular | CDS API | live (creds)‖ |
| `merra2` | NASA MERRA-2 (0.5°×0.625°, hourly) | regular | OPeNDAP | live (creds) |
| `nldas` | NLDAS-2 (0.125°, hourly, CONUS) | regular | OPeNDAP | live (creds) |
| `gpm` | GPM IMERG Daily precip (Final/Early/Late) | regular | OPeNDAP | live (creds) |
| `cmorph` | NOAA CPC CMORPH CDR daily precip (0.25°) | regular | HTTP tar NetCDF | live※ |
| `daymet` | Daymet V4R1 (1 km daily, N. America) | 2-D LCC (x/y) | OPeNDAP | live (creds) |
| `gldas` | NASA GLDAS-2 Noah (0.25°, 3-hourly, global land) | regular | OPeNDAP | live (creds)¶ |
| `fldas` | NASA FLDAS Noah (0.1°, **monthly**, global/Africa land) | regular | OPeNDAP | live (creds)⊕ |
| `nex_gddp` | NEX-GDDP-CMIP6 (0.25° daily **projections**) | regular | S3 NetCDF | live |
| `na_cordex` | NA-CORDEX (0.22°/0.44° daily **projections**, N. America) | regular | S3 Zarr | live |
| `gridmet` | gridMET daily CONUS surface meteorology (~4 km) | regular | OPeNDAP | live |
| `nclimgrid_daily` | NOAA nClimGrid-Daily (5 km, CONUS) | regular | OPeNDAP | live |
| `narr` | NOAA NARR daily monolevel fields (32 km) | 2-D LCC | OPeNDAP | live |
| `mswep` | MSWEP precipitation (0.1°, daily/3-hourly) | regular | rclone / GDrive | offline‡ |
| `em_earth` | EM-Earth (0.1° daily, global) | regular | S3 (cred-gated) | offline§ |

31 of 33 connectors are confirmed against their live stores (the auth-gated ones
with real CDS + Earthdata credentials). † `carra`/`cerra` are interpolated to a
regular grid via the CDS `grid` parameter. ‡ `mswep` is distributed only via a
GloH2O-shared Google Drive folder, reached through the external `rclone` CLI — so
it is offline-verified (path/conversion logic + a clear setup error) and needs
`rclone` + a configured Drive remote with granted access for a real fetch.
§ `em_earth`'s S3 bucket now **denies anonymous reads** (allows listing only),
so it needs AWS credentials (`config={"anon": False}`); offline-verified, and its
daily `prcp` units are **unverified** (assumed mm/day) — every precip fetch
carries an explicit warning since range-QC cannot catch a precip unit error.
¶ `gldas` reuses the Earthdata OPeNDAP mixin (same GES DISC `hydro1` host as
`nldas`); all GLDAS-2 Noah forcing fields are already canonical SI so every
mapping is identity. Live-verified against the GES DISC store (variable names,
`lat`/`lon` coords, identity mappings, bbox subset). Two products:
`gldas:noah025_3h` (GLDAS-2.1, 2000→present) and `gldas:noah025_3h_v20`
(GLDAS-2.0, 1948–2014). Wind ships as a scalar speed only (no u/v), so it maps to
`wind_speed`; opens one OPeNDAP endpoint per 3-hour stamp (8/day), so long ranges
are slow (warned in the `FetchResult`). ‖ `eobs` fills the European *observational*
gap (CFS otherwise has only reanalysis there). Unlike the other CDS connectors
E-OBS has **no server-side `area` subset**, so the full European domain is
downloaded once per variable (large, cached) and subset locally. It exposes only
the cleanly-convertible fields — `tg`→air_temperature, `rr`→precipitation_flux,
`qq`→shortwave, `fg`→wind_speed — and **defers** `pp` (sea-level, not surface,
pressure) and `hu` (relative humidity needs a surface pressure E-OBS lacks).
Request tokens are `grid_resolution` `0_1deg`/`0_25deg`, `version` `31_0e`,
`period` `full_period`. Live-verified (Netherlands bbox, 2020: T 280–295 K, precip
≤2.2e-4) once all E-OBS dataset licences were accepted on the CDS account behind
`~/.cdsapirc`. Version override via `config={"version": "30_0e"}`.
◊ `barra2` (BoM BARRA-R2, Australia) uses the anonymous NCI THREDDS **NetcdfSubset**
service: the server does the bbox+time subset and returns a clean NetCDF, avoiding
the OPeNDAP DAP2 truncation that NCI's server exhibits under concurrent reads. All
fields are CORDEX/CMIP CF names already in SI (identity mappings, incl. `pr` flux
and `huss`); no dewpoint is published. Instantaneous fields are stamped on the hour
and hourly *means* (`pr`/`rsds`/`rlds`) at the half-hour midpoint, so times are
floored to the hour to share one axis. Grid is regular `lat`/`lon` on a 0–360
longitude (requested lons are normalized). Live-verified against the NCI store.
The `wfde5`/`gridmet`/`nclimgrid_daily`/`cmorph`/`narr` batch is live-verified
end-to-end (real fetches returning physical values). ✦ `wfde5` needs the required CDS
`product` (`wfde5`) and an underscore `version` (`2_1`), confirmed against the
live form constraints; it downloads full half-degree monthly NetCDFs (one CDS
request per variable; precip = `Rainf`+`Snowf`) and subsets locally. ※ `cmorph`
reads the NOAA CPC daily-tar archive, which only hosts a **rolling recent window**
(roughly the last couple of months) — historical years are not on that endpoint,
so a fetch outside the window raises a clear "no tar listed" error. NARR carries
occasional tiny-negative precip from the source fields (advisory range-QC warning).
`hrrr` adds an `sfc_fcst` product (1-hour forecast) that provides precipitation
flux, which the analysis lacks. `gpm` adds the IMERG Early and Late near-real-time
daily runs alongside Final. `aorc_nwm` serves AORC v1.1 on the NWM v3.0 1 km LCC
grid (S3 Zarr; lat/lon generated from the LCC projection). `era5_cds` provides
standard ERA5 single-levels via the CDS API (zip of instant+accum NetCDFs merged)
as a credentialed alternative to the anonymous `era5_arco`. `narr` adds `dswrf`/
`dlwrf` (down short/longwave) radiation, live-verified against NOAA PSL (Nebraska
Jun 2020: SW 358–379, LW 353–366 W m⁻²). `na_cordex` serves North-American CORDEX
regional **climate projections** (S3 Zarr, NCAR `ncar-na-cordex`): experiments
`eval`/`hist-rcp45`/`hist-rcp85`, with grid (`NAM-22i`/`NAM-44i`) and
bias-correction (`raw`/`mbcn-Daymet`/`mbcn-gridMET`) as `config` knobs; returns the
CORDEX multi-model ensemble (`member_id` dim). `hurs` (relative humidity) is not
offered — NA-CORDEX lacks the surface pressure needed to derive specific humidity.
∇ `nwm_operational` reads the real-time NWM forcing from `noaa-nwm-pds` (S3),
generating lat/lon from the same 1 km LCC projection as `aorc_nwm`. Only the
`analysis_assim` configuration is exposed (the `tm00` analysis maps cleanly to a
valid time); the bucket keeps only a **rolling ~4-week** window, so fetches must
target recent dates. `chirts` is the temperature companion to `chirps` (UCSB CHC,
global tropics 0.05° daily, 1983–2016): `Tmax`/`Tmin` (°C) → `air_temperature`
= mean+273.15, read via HTTP byte-range from the per-year chunked NetCDFs.
`persiann_cdr` is a global daily satellite-precip CDR (NCEI, 0.25°, 1983→present
with multi-week latency); per-day files carry an unpredictable creation-date
suffix, so the connector resolves filenames from each year's directory index.
⊕ `fldas` (NASA FLDAS Noah, FEWS NET) reuses the GLDAS Earthdata/DAP4 path and is
all-identity SI, but the global product is **monthly** — good for climatology /
seasonal forcing over Africa and the global land surface, too coarse for
event-scale hydrology. Wind is a scalar speed (no u/v); land-only (ocean is fill).
⊗ `gfs` is the first **forecast** connector: global 0.25° GFS surface forcing from
the `noaa-gfs-bdp-pds` S3 GRIB2 archive. It reads the `.idx` byte-offset index and
HTTP **byte-range** fetches only the surface messages it needs (~MB, not the ~1.5 GB
whole file), decoding each with `cfgrib` (the `forecast` extra). For a requested
range it uses the most recent 00/06/12/18 UTC cycle at/before the start; valid
times map to lead hours (1-hourly to f120, 3-hourly to f384). All fields are
identity SI (u/v winds, `prate` flux); radiation/precip are interval averages,
absent at f000 (analysis). Live-verified (US, T 292–300 K, SW 80–228 W m⁻²).
⊠ `gefs` is the **ensemble** companion to `gfs` — same `.idx`/byte-range/cfgrib
machinery over the GEFS 0.25° select product (`noaa-gefs-pds`), returning a
`member` dimension (control `gec00` + perturbations `gep01`–`gep30`, selected via
`config={"members":[…]}`; default all 31). 3-hourly leads to f240 (the select
product stops there). Instantaneous fields are identity SI (air_temperature,
surface_air_pressure, eastward/northward wind). Precip (`APCP`) and radiation
(`DSWRF`/`DLWRF`) ship as 6-hour-bucket quantities (`0-3`, `0-6`, `6-9`, …) —
`APCP` accumulates, radiation averages — so they are **de-bucketed by lead hour**
(value-sign reset detection is unsafe when a fresh bucket out-rains the previous
one): `cur-prev` for accumulations, `2·cur-prev` for averages, then `APCP` → a flux
(kg m⁻² s⁻¹, like `gfs`'s `prate`) while radiation stays W m⁻². The select product
ships RH not specific humidity, so `q` is not offered. Live-verified (2 members × 4
steps: precip 0–9 mm/hr non-negative, SW correctly ~0 W m⁻² overnight after
de-averaging, member-to-member spread visible).

### Climate projections (CMIP6)

`nex_gddp` is the first **projection** connector. Because a projection has a
model × scenario × ensemble axis:
- **scenario** → product id (`nex_gddp:historical`, `nex_gddp:ssp245`,
  `nex_gddp:ssp585`, …);
- **model / member** → connector config, e.g.
  `get_connector("nex_gddp")(config={"model": "MPI-ESM1-2-HR", "member": "r1i1p1f1"})`
  (default `ACCESS-CM2` / `r1i1p1f1`);
- the chosen model/scenario/member are recorded in `FetchResult.provenance` and
  on the returned dataset's attrs (`cmip6_model`/`cmip6_scenario`/`cmip6_member`).

```bash
pip install -e '.[climate,cds,earthdata]'
```

CDS connectors need `~/.cdsapirc`; Earthdata connectors (`merra2`, `nldas`) need
`EARTHDATA_TOKEN` (or `~/.netrc` / `EARTHDATA_USERNAME`+`PASSWORD`) and the "NASA
GESDISC DATA ARCHIVE" app authorized under your URS profile. `nldas` opens one
OPeNDAP endpoint **per hour** — fine for short windows, slow for long ranges
(warned in `FetchResult`).

Migration backlog (port from SYMFLUENCE). Infra now available:
`cfs.subset.grid2d` (2-D/projected grids), `cfs.subset.deaccumulate`
(reset-aware), `cfs.connectors.protocols.cds_api`, `cfs.derive.humidity`
(RH→q), the analysis+forecast merge in `cfs.connectors.cds_reanalysis`, and
`cfs.connectors.protocols.earthdata` (URS-authenticated OPeNDAP).

- **Anonymous, regular grid**: `em_earth` — blocked on confirming `prcp` units
- **Earthdata OPeNDAP** (mixin exists): `daymet` (needs `grid2d` + derived
  LW/pressure/wind/humidity)
- **Other access**: `mswep` (Google Drive / rclone)

## Hardening / robustness

- **Range QC** (`cfs/qc.py`): every fetch samples the harmonized cube against
  each canonical variable's physical `valid_range` and reports out-of-range
  values in `FetchResult.warnings` — catching unit-conversion bugs (a precip
  flux of 8.6 instead of 1e-4) before they reach a model. Advisory; never fails
  a fetch. Toggle with `CFS_QC_ENABLED`.
- **Fetch guardrails**: shared `_guard_area` (`CFS_MAX_AREA_DEG2`) and
  cell-count (`CFS_MAX_CELLS_PER_FETCH`) checks on the base class refuse
  accidental continental/decadal pulls; enforced uniformly via `_finalize`.
- **Reset-aware de-accumulation** (`cfs/subset/deaccumulate.py`): running-total
  fields (ERA5-Land `tp`/`ssrd`/`strd`) are converted to per-step increments
  before unit conversion, handling daily resets.

## Derived variables

When a provider lacks a canonical field, CFS derives it once, in a tested place
(`cfs/derive/`). Currently: **specific humidity from relative humidity**
(`cfs/derive/humidity.py`, Bolton 1980 saturation vapour pressure) — used by
CARRA/CERRA, which ship 2 m RH rather than specific humidity. Derivation inputs
(RH) are consumed, not emitted: they do not appear in the canonical output.

## Tests

```bash
pytest -m 'not network'    # offline: harmonization + subsetting logic
pytest -m network          # integration: real ERA5 fetch from GCS
```

## Naming note

"CFS" also denotes NOAA's **Climate Forecast System** (CFSR/CFSv2), itself a
forcing product. If a CFSR connector is ever added it must use a disambiguated
slug (e.g. `cfsr`) to avoid collision with the service name.
