# SYMFLUENCE Integration

CFS ships a [SYMFLUENCE](https://github.com/DarriEy/SYMFLUENCE) plugin that
makes **every CFS product available as a SYMFLUENCE forcing dataset** — one
config key instead of one bespoke acquisition handler per product. The plugin
lives entirely in CFS (`cfs.integrations.symfluence`); SYMFLUENCE needs no
CFS-specific code beyond accepting `FORCING_DATASET: CFS`.

## How it works

SYMFLUENCE discovers external plugins through the `symfluence.plugins`
[entry-point group](https://packaging.python.org/en/latest/specifications/entry-points/).
CFS declares one in its `pyproject.toml`:

```toml
[project.entry-points."symfluence.plugins"]
cfs = "cfs.integrations.symfluence:register"
```

On `import symfluence`, the framework's bootstrap finds the entry point and
calls the zero-arg `register()`, which adds two handlers to SYMFLUENCE's
registries:

| Component | Registry | Key | Job |
| --- | --- | --- | --- |
| `CFSForcingAcquirer` | acquisition handlers | `CFS` | download via [`cfs.fetch_sync`](python-api.md), write one canonical NetCDF |
| `CFSDatasetHandler` | dataset handlers | `cfs` | rename canonical-v1 → CFIF before HRU remapping |

No manual registration, no import of `cfs` in your scripts — installing the
two packages side by side is the whole setup. The dependency points only one
way: CFS never imports SYMFLUENCE at `import cfs` time (the integration
module degrades gracefully when SYMFLUENCE is absent), and SYMFLUENCE is
**not** a CFS dependency.

## Install

```bash
pip install symfluence community-forcing-service
```

or, once SYMFLUENCE ships the extra:

```bash
pip install "symfluence[cfs]"
```

Verify the plugin registered:

```bash
python -c "import symfluence; \
  from symfluence.data.acquisition.registry import AcquisitionRegistry as A; \
  print('CFS' in [n.upper() for n in A.list_datasets()])"
# True
```

## Configuration

```yaml
# SYMFLUENCE config (YAML) — forcing section
FORCING_DATASET: CFS

# Required: a CFS product id ("provider:product", as `cfs fetch -P` takes),
# or a bare provider slug when the provider offers exactly one product.
CFS_PRODUCT: era5_arco:single_levels

# Optional: comma-separated canonical variable names (default: all the
# product offers). See the canonical-v1 vocabulary.
CFS_VARIABLES: air_temperature, precipitation_flux, surface_downwelling_shortwave_flux

# Optional: provider-specific connector configuration dict
# (e.g. ensemble members for GEFS).
CFS_CONNECTOR_CONFIG:
  members: [gec00]
```

The bounding box and time range come from the standard SYMFLUENCE domain
keys (`BOUNDING_BOX_COORDS`, `EXPERIMENT_TIME_START` / `EXPERIMENT_TIME_END`)
— nothing CFS-specific to repeat.

## What happens downstream

```
cfs.fetch_sync()                SYMFLUENCE
canonical-v1 xr.Dataset   ──▶   raw_data/*.nc        (CFSForcingAcquirer)
                          ──▶   CFIF rename + attrs  (CFSDatasetHandler)
                          ──▶   EASYMORE HRU remap   (SYMFLUENCE resampling)
                          ──▶   model-ready forcing  (SUMMA, FUSE, …)
```

1. **Acquire** — `CFSForcingAcquirer.download()` calls `cfs.fetch_sync()`
   with the domain bbox/time range and streams the (dask-lazy) canonical
   dataset to a compressed NetCDF:
   `domain_{name}_cfs_{product}_{start}_{end}.nc`. Re-runs skip the download
   if the file exists (`FORCE_DOWNLOAD: true` overrides).
2. **Standardize** — `CFSDatasetHandler` renames canonical-v1 variables to
   SYMFLUENCE's CFIF vocabulary. Because both vocabularies use CF-aligned
   names in SI units, the mapping is the **identity** for all nine shared
   variables and no unit conversion or de-accumulation happens here (CFS
   already guarantees fluxes, never accumulations). `dewpoint_temperature`
   has no CFIF counterpart and passes through unchanged.
3. **Remap** — SYMFLUENCE's standard EASYMORE pipeline remaps the grid to
   HRUs and its model adapters write model-specific forcing files. None of
   that is CFS's job (see [the boundary](index.md)).

## v1 limitation: regular lat/lon grids only

The dataset handler supports products on **regular latitude/longitude
grids** (1-D `latitude` / `longitude` dimensions — most of the
[catalog](catalog.md)). Projected / curvilinear products (`rdrs`, `conus404`,
`hrrr`, `daymet`, `narr`, `aorc_nwm`, `nwm_operational` — native `rlat`/`rlon`
or `y`/`x` dims with 2-D lat/lon coordinates per the
[canonical-v1 spec](canonical-v1.md)) raise `NotImplementedError` at the
preprocessing step. SYMFLUENCE's native handlers for those products remain
the supported path until the plugin grows projected-grid support.
