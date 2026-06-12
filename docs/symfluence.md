# SYMFLUENCE Integration

CFS ships a [SYMFLUENCE](https://github.com/DarriEy/SYMFLUENCE) plugin
(`cfs.integrations.symfluence`) that makes CFS a **drop-in acquisition
backend** for SYMFLUENCE's forcing pipeline: keep your existing config
(`FORCING_DATASET: ERA5`, the same bbox/time keys) and flip one switch —

```yaml
DATA_ACCESS: community
```

— to have CFS do the acquisition underneath. A second, parallel-name mode
(`FORCING_DATASET: CFS`) exposes the *entire* CFS catalog, including products
SYMFLUENCE has no native handler for. The plugin lives entirely in CFS;
SYMFLUENCE is **not** a CFS dependency and CFS never imports SYMFLUENCE at
`import cfs` time.

## Install

```bash
pip install symfluence community-forcing-service
```

or, once SYMFLUENCE ships the extra:

```bash
pip install "symfluence[cfs]"
```

SYMFLUENCE discovers the plugin through the `symfluence.plugins` entry-point
group on `import symfluence` — no manual registration, no `import cfs` in
your scripts.

## Drop-in mode: shadow wrappers

At registration time (which runs *after* SYMFLUENCE's in-tree handlers
register), the plugin captures each native acquisition class and re-registers
a thin wrapper under the **same name**. The wrapper decides per `download()`
call:

- **Default (`DATA_ACCESS` unset, `MAF`, or `cloud`)** — the captured native
  class is instantiated and runs **unchanged**. Installing the plugin without
  enabling it is bit-identical to not having it.
- **`DATA_ACCESS: community`** — the data is fetched via
  [`cfs.fetch_sync`](python-api.md) from the mapped CFS product and written
  as canonical-v1 NetCDF into the same raw-data location (the `cfs_schema`
  global attribute marks the files for the preprocessing step). Skip-if-exists
  and `FORCE_DOWNLOAD` semantics are preserved.

`community` means "use the community service for every step it covers; behave
like `MAF` elsewhere" — in particular, steps with a cloud-only fast path fall
back to their MAF behavior, which users coming from `DATA_ACCESS: cloud`
should know.

### The shadow map (parity-gated)

Only datasets whose native-vs-community output was **live-validated**
(2026-06-11, native and CFS reading the same upstream archives) are shadowed:

| Native name(s) | CFS product | Measured parity |
| --- | --- | --- |
| `ERA5` | `era5_arco:single_levels` | value-identical; the 3 accumulation→flux variables differ ≤ 2 float32 ulps (op-order only, see below) |
| `NLDAS`, `NLDAS2`, `NLDAS-2` | `nldas:fora0125_h` | bitwise-identical (7/8 variables); precipitation ≤ 1 float32 ulp |
| `AORC` | `aorc:conus_1km` | bit-identical |
| `NEX-GDDP-CMIP6`, `NEX-GDDP` | `nex_gddp:<scenario>` | bit-identical (same physical files; NCCS THREDDS vs S3 mirror) |

**ERA5 fine print:** both sides read the same ARCO Zarr bytes. Instantaneous
variables and coordinates are bitwise equal, and the SYMFLUENCE-derived
`wind_speed` / `specific_humidity` are recomputed by the shadow with the
native float32 op order (bitwise equal). The three accumulation→flux
conversions (precipitation, SW/LW radiation) differ by ≤ 2 float32 ulps
(≤ 1.33 × 10⁻⁷ relative) purely from operation order: native computes
`(x / 3600) · k` in two roundings, CFS computes the fused `x · (k / 3600)` in
one (single-rounding, slightly more accurate). We deliberately do not contort
CFS's conversion to chase the last ulps of the native double-rounding.

### Excluded datasets (and why)

- **`MSWEP`, `EM-EARTH`** — *not shadowed* until live parity validation is
  possible. Both are currently blocked on this side (no rclone Google Drive
  remote for MSWEP; the EM-Earth S3 bucket denies anonymous GET), so the
  parity gate cannot be measured. Their native handlers keep running
  untouched under every `DATA_ACCESS` value.
- **Projected/curvilinear-grid datasets** (`CARRA`, `CERRA`, `RDRS`,
  `CONUS404`, `HRRR`, `DAYMET`, `NWM3_RETROSPECTIVE`, `CASR`) — not shadowed
  in v1; the CFS dataset handler supports regular lat/lon grids only.
  Enabling them later is purely additive in the plugin.

### Per-dataset opt-out

A flat `<NATIVE_NAME>_BACKEND` key overrides the global gate per dataset:

```yaml
DATA_ACCESS: community     # community everywhere it's covered...
ERA5_BACKEND: native       # ...but keep native ERA5 acquisition
```

Keys: `ERA5_BACKEND`, `NLDAS_BACKEND` (also `NLDAS2_BACKEND` /
`NLDAS_2_BACKEND`), `AORC_BACKEND`, `NEX_GDDP_BACKEND` (also
`NEX_GDDP_CMIP6_BACKEND`). Value `native` forces the captured native class;
`community` forces CFS for that dataset alone.

### NEX-GDDP specifics

The community fetch is built from the **same config keys the native handler
reads**: `NEX_MODELS` (required), `NEX_SCENARIOS` (default `[historical]`),
`NEX_ENSEMBLES` (default `[r1i1p1f1]`), `NEX_VARIABLES`. One canonical NetCDF
is written per model × scenario × member; the experiment window is clipped to
each scenario's extent (e.g. `historical` ≤ 2014). Because NEX-GDDP publishes
no surface pressure, the shadow fabricates the same constant
`p0 · exp(−z/H)` pressure the native handler does (set `DOMAIN_MEAN_ELEV_M`
for an elevation-adjusted value). `NEX_VARIABLES` entries without a
canonical-v1 counterpart (`hurs`, `tasmax`, `tasmin`) are skipped with a loud
warning.

### Self-detecting preprocessing

The plugin also shadows the matching dataset-handler keys (`era5`, `aorc`,
`nex-gddp`, `nex-gddp-cmip6`) with **self-detecting** wrappers: every method
that touches raw files first checks one file's `cfs_schema` attribute —
canonical-v1 files take the single canonical→CFIF rename, native-format files
delegate to the captured native dataset handler. Detection is per-file in
`process_dataset`, so a domain can mix cached native raw files with newly
community-acquired ones.

`NLDAS` is special: SYMFLUENCE has **no native NLDAS dataset handler** (only
a variable rename map), so the plugin registers the CFS canonical handler
under `nldas`/`nldas2`/`nldas-2` outright. Those keys serve
**community-acquired canonical files only**; native-format NLDAS raw files
raise a clear error instead of being mis-processed. (`era5_cds` keeps its
native handler — its raw layout is CDS-specific.)

## Parallel-name mode: the full CFS catalog

For CFS products with no SYMFLUENCE equivalent (GEFS, GFS, MERRA2, CHIRPS,
GridMET, E-OBS, BARRA2, …), select CFS by name:

```yaml
FORCING_DATASET: CFS

# Required: a CFS product id ("provider:product", as `cfs fetch -P` takes),
# or a bare provider slug when the provider offers exactly one product.
CFS_PRODUCT: gefs:atmos_0p25

# Optional: comma-separated canonical variable names (default: all the
# product offers). See the canonical-v1 vocabulary.
CFS_VARIABLES: air_temperature, precipitation_flux

# Optional: provider-specific connector configuration dict
# (e.g. ensemble members for GEFS).
CFS_CONNECTOR_CONFIG:
  members: [gec00]
```

The bounding box and time range come from the standard SYMFLUENCE domain
keys (`BOUNDING_BOX_COORDS`, `EXPERIMENT_TIME_START` / `EXPERIMENT_TIME_END`)
— nothing CFS-specific to repeat. This mode requires `'CFS'` in SYMFLUENCE's
`FORCING_DATASET` schema (shipped in SYMFLUENCE ≥ 0.9.1).

## What happens downstream

```
cfs.fetch_sync()                SYMFLUENCE
canonical-v1 xr.Dataset   ──▶   raw_data/*.nc        (shadow wrapper / CFSForcingAcquirer)
                          ──▶   CFIF rename + attrs  (self-detecting dataset handler)
                          ──▶   EASYMORE HRU remap   (SYMFLUENCE resampling)
                          ──▶   model-ready forcing  (SUMMA, FUSE, …)
```

Both CFS's canonical-v1 vocabulary and SYMFLUENCE's CFIF use CF-aligned names
in SI units, so the rename is the **identity** for all nine shared variables
and no unit conversion or de-accumulation happens in the handler (CFS already
guarantees fluxes, never accumulations). `dewpoint_temperature` has no CFIF
counterpart and passes through unchanged.

## Verify the plugin registered

```bash
python -c "import symfluence; \
  from symfluence.core.registries import R; \
  cls = R.acquisition_handlers.get('ERA5'); \
  print('shadow installed:', getattr(cls, '_cfs_shadow', False))"
# shadow installed: True
```

## v1 limitation: regular lat/lon grids only

The dataset handler supports products on **regular latitude/longitude
grids** (1-D `latitude` / `longitude` dimensions — most of the
[catalog](catalog.md)). Projected / curvilinear products (`rdrs`, `conus404`,
`hrrr`, `daymet`, `narr`, `aorc_nwm`, `nwm_operational` — native `rlat`/`rlon`
or `y`/`x` dims with 2-D lat/lon coordinates per the
[canonical-v1 spec](canonical-v1.md)) raise `NotImplementedError` at the
preprocessing step. SYMFLUENCE's native handlers for those products remain
the supported path until the plugin grows projected-grid support.
