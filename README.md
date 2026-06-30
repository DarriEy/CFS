# CFS — Community Forcing Service

Acquire-and-subset access to meteorological **forcing** products for hydrological
modelling.

Acquiring forcing for a modeling study traditionally means bespoke scripting
per product — every product has its own API, native variable names, units,
accumulation conventions, and grid, and the accumulation-to-rate conversion is
re-implemented (and mis-implemented) in every group's scripts. CFS replaces
that with **one async interface over 33 products** that stops at a canonical,
CF-aligned `xarray.Dataset` — deliberately leaving catchment/HRU remapping and
model-specific file formats to modeling frameworks (e.g. SYMFLUENCE).

**Documentation: <https://darriey.github.io/CFS/>**

CFS is the third member of the community-data triad alongside
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
The output contract (names, units, attrs, grid layouts, time conventions) is
specified normatively in
[the canonical-v1 spec](https://darriey.github.io/CFS/canonical-v1/).

## Install

```bash
pip install 'community-forcing-service[climate]'   # xarray, zarr, gcsfs, dask, netcdf4
```

The distribution is named `community-forcing-service` (the name `cfs` is taken
on PyPI), but the import package and CLI are still `cfs` (`import cfs`). From a
checkout:

```bash
pip install -e '.[climate]'
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
import cfs

ds, result = cfs.fetch_sync(
    "era5_arco:single_levels",
    bbox=(-114.5, 50.7, -114.0, 51.1),
    time_range=("2015-06-01T00:00", "2015-06-01T06:00"),
    variables=["air_temperature", "precipitation_flux"],
)
# ds: lazy canonical cube;  result: FetchResult provenance/shape metadata
```

From async code, `await cfs.fetch(...)` directly. Runtime settings (cache
dir, timeouts, guardrails) can be overridden after import with
`cfs.configure(...)`. The lower-level
`discover()` / `get_connector(slug)` / async-context-manager seam stays
public — see the
[Python API guide](https://darriey.github.io/CFS/python-api/).

## Using CFS inside SYMFLUENCE

CFS ships a SYMFLUENCE plugin (`cfs.integrations.symfluence`, auto-discovered
via the `symfluence.plugins` entry point): install both packages, set
`FORCING_DATASET: CFS` and `CFS_PRODUCT: <provider:product>` in a SYMFLUENCE
config, and every CFS product becomes a SYMFLUENCE forcing dataset — CFS
acquires the canonical cube, SYMFLUENCE renames it to CFIF and does the HRU
remapping. See the
[SYMFLUENCE integration guide](https://darriey.github.io/CFS/symfluence/).

## Adding a connector

Subclass `BaseForcingConnector` (optionally mix in `ZarrStoreMixin`), implement
`list_products()` and `fetch()`, declare a `VariableMapping` table mapping native
names → canonical vars + linear unit conversions, and decorate with
`@register("slug")`. `discover()` finds it automatically.

## Providers

33 connectors — 31 live-verified against their upstream stores (19 anonymous +
12 auth-gated, confirmed with real CDS and Earthdata credentials); `mswep` and
`em_earth` are offline-verified pending access/credentials. Highlights:

| | products |
|---|---|
| **Global / regional reanalyses** | ERA5 (ARCO + CDS), ERA5-Land, MERRA-2, CARRA, CERRA, RDRS/CaSR, BARRA-R2, CONUS404, NARR, WFDE5 |
| **Analysis / observation grids** | AORC (+ NWM grid), NLDAS-2, HRRR, NWM operational, Daymet, gridMET, nClimGrid-Daily, GLDAS, FLDAS, E-OBS |
| **Satellite / merged precipitation** | CHIRPS, CHIRTS, GPM IMERG, PERSIANN-CDR, CMORPH, MSWEP, EM-Earth |
| **Forecasts** | GFS (deterministic), GEFS (ensemble, `member` dimension) |
| **Climate projections** | NEX-GDDP-CMIP6, NA-CORDEX |

The full per-provider table — grid type, access protocol, auth, verification
status, and the per-provider caveats (rolling archive windows, unverified
units, slow OPeNDAP paths, derivation notes) — lives in the
[provider catalog](https://darriey.github.io/CFS/catalog/), with the
machine-readable version in
[`inventory/providers.yaml`](inventory/providers.yaml).

CDS connectors need `~/.cdsapirc`; Earthdata connectors need
`EARTHDATA_TOKEN` (or `~/.netrc` / `EARTHDATA_USERNAME`+`PASSWORD`) with the
"NASA GESDISC DATA ARCHIVE" app authorized. GFS/GEFS need the `forecast`
extra:

```bash
pip install 'community-forcing-service[climate,cds,earthdata,forecast]'
```

Note that CFS is a passthrough service — every fetch hits the provider's live
store, so transient upstream outages (THREDDS restarts, S3 hiccups, CDS queue
congestion) can surface as fetch errors independent of CFS itself.

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

## Automated CI triage

When CI fails on `main`, a Claude Code agent (`.github/workflows/ci-autotriage.yml`) reads the
failure, posts a triage report as a commit comment, and classifies it:

| Classification | Action |
| --- | --- |
| `adapter_drift` / `data_drift` — a data provider changed; fix confined to `connectors/`/`tests/` | fix PR labeled `automerge-on-green`, **auto-merged once CI passes** |
| `contract_change` — touches `src/cfs/core/` | PR labeled `needs-human-review` (a human merges) |
| `tooling_drift` — build / CI / dependency / packaging | PR labeled `needs-human-review` (a human merges) |
| `outage` / `real_bug` / `other` | report only, no code change |

**Safety:** the auto-merge workflow (`autofix-automerge.yml`) merges a PR only if its entire diff is
within `connectors/`/`tests/` — a misclassified change can never auto-merge, regardless of label.
Claude authenticates via the `ANTHROPIC_API_KEY_OAUTH` repo secret. Pause anytime with
`gh workflow disable "CI Auto-Triage" -R DarriEy/CFS`.

Labels: `claude-autofix` (agent-opened) · `automerge-on-green` (drift fix, self-merges on green) ·
`needs-human-review` (needs a human).

## Naming note

"CFS" also denotes NOAA's **Climate Forecast System** (CFSR/CFSv2), itself a
forcing product. If a CFSR connector is ever added it must use a disambiguated
slug (e.g. `cfsr`) to avoid collision with the service name.
