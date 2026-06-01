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

Implemented — **all 14 live-verified** (7 anonymous; 7 auth-gated confirmed with
real CDS + Earthdata credentials):

| slug | product | grid | access | verified |
|------|---------|------|--------|----------|
| `era5_arco` | ECMWF ERA5 (0.25°, hourly) | regular | GCS Zarr | live |
| `aorc` | NOAA AORC v1.1 (1 km, hourly) | regular | S3 Zarr | live |
| `chirps` | CHIRPS v2.0 daily precip (0.05°) | regular | HTTP NetCDF | live |
| `rdrs` | RDRS / CaSR v3.2 (Canada, ~10 km, hourly) | 2-D rotated pole | OPeNDAP | live |
| `conus404` | CONUS404 (4 km WRF, hourly) | 2-D LCC | OSN Zarr | live |
| `hrrr` | NOAA HRRR analysis (3 km, hourly) | 2-D LCC | hrrrzarr S3 | live |
| `era5_land` | ECMWF ERA5-Land (0.1°, hourly) | regular | CDS API | live (creds) |
| `carra` | Copernicus Arctic Regional Reanalysis (2.5 km) | regular† | CDS API | live (creds) |
| `cerra` | Copernicus European Regional Reanalysis (5.5 km) | regular† | CDS API | live (creds) |
| `merra2` | NASA MERRA-2 (0.5°×0.625°, hourly) | regular | OPeNDAP | live (creds) |
| `nldas` | NLDAS-2 (0.125°, hourly, CONUS) | regular | OPeNDAP | live (creds) |
| `gpm` | GPM IMERG Final daily precip (0.1°) | regular | OPeNDAP | live (creds) |
| `daymet` | Daymet V4R1 (1 km daily, N. America) | 2-D LCC (x/y) | OPeNDAP | live (creds) |
| `nex_gddp` | NEX-GDDP-CMIP6 (0.25° daily **projections**) | regular | S3 NetCDF | live |
| `mswep` | MSWEP precipitation (0.1°, daily/3-hourly) | regular | rclone / GDrive | offline‡ |
| `em_earth` | EM-Earth (0.1° daily, global) | regular | S3 (cred-gated) | offline§ |

14 of 16 connectors are confirmed against their live stores (the auth-gated ones
with real CDS + Earthdata credentials). † `carra`/`cerra` are interpolated to a
regular grid via the CDS `grid` parameter. ‡ `mswep` is distributed only via a
GloH2O-shared Google Drive folder, reached through the external `rclone` CLI — so
it is offline-verified (path/conversion logic + a clear setup error) and needs
`rclone` + a configured Drive remote with granted access for a real fetch.
§ `em_earth`'s S3 bucket now **denies anonymous reads** (allows listing only),
so it needs AWS credentials (`config={"anon": False}`); offline-verified, and its
daily `prcp` units are **unverified** (assumed mm/day) — every precip fetch
carries an explicit warning since range-QC cannot catch a precip unit error.

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
