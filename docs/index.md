# CFS — Community Forcing Service

**One asynchronous interface to 46 meteorological forcing providers, returning a
canonical, CF-aligned `xarray.Dataset`.**

## Statement of need

Every gridded hydrological or land-surface modeling study begins by acquiring
meteorological forcing — precipitation, temperature, humidity, wind, pressure,
and radiation — and in practice this means bespoke scripting for each product.
ERA5 lives in a cloud-optimized Zarr store, NLDAS behind Earthdata-authenticated
OPeNDAP, CARRA behind the CDS API, GFS in S3 GRIB2 files read by byte-range;
each product has its own native variable names, units, accumulation conventions
(running totals, per-interval buckets, instantaneous rates), grid (regular
lat/lon, rotated pole, Lambert conformal), and authentication scheme. The
conversion that most often goes wrong — accumulated precipitation or radiation
to a flux — is reimplemented, slightly differently, in every group's scripts.
CFS replaces this per-product scripting with a single asynchronous interface:
every connector acquires its product, subsets it to a bounding box and time
range, and harmonizes it to one canonical, CF-aligned schema
([`canonical-v1`](canonical-v1.md)) in which precipitation and radiation are
always rates, never accumulations. CFS deliberately stops there: remapping to
catchments/HRUs and writing model-specific forcing files are left to modeling
frameworks (e.g. SYMFLUENCE), which is what keeps the service reusable across
them.

## The community-data triad

CFS is the third member of a triad of focused data services:

| Service | Data | Returns |
|---------|------|---------|
| [CAS](https://github.com/DarriEy/CAS)   | geospatial attributes (DEM, soil, land cover) | harmonized zonal statistics |
| [CSFS](https://github.com/DarriEy/CSFS) | streamflow observations | harmonized station time series |
| **CFS** | **meteorological forcing** | **canonical, subset `xarray.Dataset`** |

## What CFS does

```
 upstream store ──▶  subset to bbox+time  ──▶  harmonize to canonical  ──▶  xr.Dataset
   (Zarr/S3/…)        cfs.subset.bbox            cfs.subset.canonical          │
                                                                               ▼
                                              [ consumer: HRU remap + model schema ]
```

It deliberately does **not**:

- remap to HRUs / sub-basins,
- write model-specific forcing schemas (SUMMA, FUSE, mizuRoute, …),
- serialize monthly NetCDF chunks or handle HPC filesystem locking.

Those steps are model- and deployment-specific, so they stay in the consumer.

## Features

- **47 connectors** spanning global and regional reanalyses (ERA5/ERA5-Land,
  MERRA-2, CARRA, CERRA, RDRS, BARRA2, CONUS404, NARR, WFDE5), analysis
  products (AORC, NLDAS, HRRR, NWM operational, Daymet, gridMET,
  nClimGrid-Daily, GLDAS, FLDAS), satellite/station precipitation and
  temperature (CHIRPS, CHIRTS, GPM IMERG, PERSIANN-CDR, CMORPH, MSWEP,
  EM-Earth, E-OBS), forecasts (GFS deterministic, GEFS ensemble with a
  `member` dimension), and climate projections (NEX-GDDP-CMIP6, NA-CORDEX).
  See the [Provider Catalog](catalog.md).
- **Canonical harmonization** to CF-aligned names and SI units — the
  [canonical-v1](canonical-v1.md) contract downstream frameworks code against.
- **Subsetting** for regular lat/lon grids (antimeridian-safe) and
  2-D/projected grids (rotated pole, Lambert conformal).
- **Reset-aware and bucket-aware de-accumulation** of running-total and
  interval-bucket precipitation/radiation fields.
- **Derived variables** where a product lacks a canonical field (specific
  humidity from RH/T/P, Bolton 1980).
- **Advisory range QC** on every fetch, catching unit-conversion errors before
  they reach a model.
- **Fetch guardrails** (bbox-area and cell-count limits) enforced uniformly on
  the connector base class.
- **Lazy by default** — connectors return dask-backed datasets where the
  protocol allows, so the caller decides when to materialize.
- A **[CLI](cli.md)** (`cfs providers` / `cfs products` / `cfs fetch`) and an
  in-process **[Python API](python-api.md)**.

## Where to next

- New here? Start with the [Quick Start](quickstart.md).
- Choosing a product? Browse the [Provider Catalog](catalog.md).
- Consuming CFS output from a framework? Read the
  [canonical-v1 specification](canonical-v1.md) — it is the normative output
  contract.
- Extending CFS? See the
  [contributing guide](https://github.com/DarriEy/CFS/blob/main/CONTRIBUTING.md)
  and the [API Reference](reference.md).

## Naming note

"CFS" also denotes NOAA's **Climate Forecast System**. Its historical CFSR and
recent CFSv2/CDAS products use the disambiguated `cfsr` and `cfsv2` provider
slugs to avoid collision with the service name.
