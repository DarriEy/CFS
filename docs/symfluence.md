# SYMFLUENCE Integration

CFS ships a [SYMFLUENCE](https://github.com/DarriEy/SYMFLUENCE) plugin
(`cfs.integrations.symfluence`) that registers CFS as a formal **acquisition
backend** under SYMFLUENCE's versioned `AcquisitionBackend` protocol
(`symfluence.data.backends.contract`). Keep your existing config
(`FORCING_DATASET: ERA5`, the same bbox/time keys) and flip one switch —

```yaml
DATA_ACCESS: community
```

— to have CFS do the acquisition underneath. The plugin lives entirely in
CFS; SYMFLUENCE is **not** a CFS dependency and CFS never imports SYMFLUENCE
at `import cfs` time.

## Install

```bash
pip install symfluence community-forcing-service
```

SYMFLUENCE discovers the plugin through the `symfluence.plugins` entry-point
group on `import symfluence` — no manual registration, no `import cfs` in
your scripts.

## How it works: the protocol

`register()` adds exactly two things:

1. **`CommunityForcingBackend`** in `R.acquisition_backends['community']`.
   The backend *declares* what it can serve (`capabilities()`: dataset ids,
   grid class, CFIF variables, auth, temporal coverage, **parity grade**) and
   SYMFLUENCE's selection layer decides per request who acquires:
   `DATA_ACCESS: community` → priority `[community, native]`;
   `cloud`/`MAF` → native only; a per-dataset `<NAME>_BACKEND: native|community`
   key pins the choice. A backend can decline at capability time (unclaimed
   dataset, unservable variables, window outside coverage) → clean fallthrough
   to native with an INFO log. **No registry overwriting, no captured native
   classes, no file sniffing** — the shadow-wrapper machinery of plugin
   versions ≤ 0.4 is gone.
2. **`CanonicalV1Handler`** in `R.dataset_handlers['canonical-v1']` — ONE
   schema-keyed preprocessing handler for every canonical-v1 file. The
   backend's `acquire()` writes a sidecar `acquisition_manifest.json` next to
   the raw files declaring the output schema; SYMFLUENCE's forcing
   preprocessing dispatches on that declared schema. Raw directories without
   a manifest are legacy native data and take the per-dataset native path,
   bit-identical.

Failures are mapped onto the protocol error taxonomy
(`AuthRequired`, `WindowOutOfRange`, `UpstreamOutage`, `IntegrityError`, …),
so the framework's retry/fallback logic keys off exception classes, never
message text.

## The capability table (parity-gated)

Only datasets whose native-vs-community output was **live-validated**
(2026-06-11/12, native and CFS reading the same upstream archives) carry a
parity grade. The framework refuses ungraded (`parity_grade: None`) datasets
from a non-native backend unless `ALLOW_UNGATED_BACKENDS: true`.

| Dataset id(s) | CFS product | Grid class | Parity grade |
| --- | --- | --- | --- |
| `ERA5` | `era5_arco:single_levels` | regular lat/lon | `value-identical:2ulp` — the 3 accumulation→flux variables differ ≤ 2 float32 ulps (op-order only, see below) |
| `NLDAS`, `NLDAS2`, `NLDAS-2` | `nldas:fora0125_h` | regular lat/lon | `value-identical:1ulp` — 7/8 variables bitwise; precipitation ≤ 1 float32 ulp |
| `AORC` | `aorc:conus_1km` | regular lat/lon | `bit-identical` (declines pre-2002 windows: the native NWM-projected fallback serves those) |
| `NEX-GDDP-CMIP6`, `NEX-GDDP` | `nex_gddp:<scenario>` | regular lat/lon | `bit-identical` (same physical files; NCCS THREDDS vs S3 mirror) |
| `RDRS`, `RDRS_v3.1` | `rdrs:casr_v32` | **projected** (rotated pole) | `bit-identical` (exp10: all 9 variables + rlat/rlon + 2-D lat/lon + time bitwise) |
| `CFS` | from `options={'product': …}` / `CFS_PRODUCT` | varies | *ungraded* (`None`) — exercises the ungated policy |

**ERA5 fine print:** both sides read the same ARCO Zarr bytes. Instantaneous
variables and coordinates are bitwise equal, and the SYMFLUENCE-derived
`wind_speed` / `specific_humidity` are recomputed by the backend with the
native float32 op order (bitwise equal). The three accumulation→flux
conversions (precipitation, SW/LW radiation) differ by ≤ 2 float32 ulps
(≤ 1.33 × 10⁻⁷ relative) purely from operation order.

**RDRS fine print:** the canonical store carries the wind primitives
(`uas`/`vas`); the canonical-v1 handler derives
`wind_speed = hypot(eastward_wind, northward_wind)` during preprocessing.
This composite deviates ≤ 9 × 10⁻⁴ m/s (max, exp10 measurement) from CaSR's
own `sfcWind` diagnostic, which is computed upstream with different
physics-level rounding — physically negligible and documented rather than
chased.

**Excluded:** `MSWEP` and `EM-EARTH` are *not claimed* until live parity
validation is possible (blocked: no rclone Google Drive remote for MSWEP; the
EM-Earth S3 bucket denies anonymous GET). Their native handlers keep running
untouched under every `DATA_ACCESS` value. Other projected datasets (CARRA,
CERRA, CONUS404, HRRR, …) follow RDRS once their parity experiments land.

## Per-dataset opt-out

A flat `<NATIVE_NAME>_BACKEND` key overrides the global gate per dataset:

```yaml
DATA_ACCESS: community     # community everywhere it's covered...
ERA5_BACKEND: native       # ...but keep native ERA5 acquisition
```

## Projected grids (RDRS first)

RDRS is the first community dataset on a projected grid. The canonical-v1
layout keeps the native `rlat`/`rlon` dims with 2-D `latitude`/`longitude`
auxiliary coordinates (see the [canonical-v1 spec](canonical-v1.md));
`CanonicalV1Handler`:

- reports coordinate names `('latitude', 'longitude')` (EASYMORE handles 1-D
  and 2-D coords by name),
- splits the consolidated canonical store into native-pipeline-style monthly
  files (`RDRS_monthly_YYYYMM.nc`, complete hourly axis, gap-filled exactly
  like the native consolidated path),
- builds the forcing-grid shapefile with one polygon per native cell from the
  2-D coordinate corners — ported from the proven native RDRS implementation
  (the grids were verified bitwise identical in exp10, so the geometry
  matches).

## NEX-GDDP specifics

The community fetch is built from the **same config keys the native handler
reads**: `NEX_MODELS` (required), `NEX_SCENARIOS` (default `[historical]`),
`NEX_ENSEMBLES` (default `[r1i1p1f1]`), `NEX_VARIABLES`. One canonical NetCDF
is written per model × scenario × member; the experiment window is clipped to
each scenario's extent (e.g. `historical` ≤ 2014). Because NEX-GDDP publishes
no surface pressure, the backend fabricates the same constant
`p0 · exp(−z/H)` pressure the native handler does (set `DOMAIN_MEAN_ELEV_M`
for an elevation-adjusted value).

## Parallel-name mode: the full CFS catalog

For CFS products with no SYMFLUENCE equivalent (GEFS, GFS, MERRA2, CHIRPS,
GridMET, E-OBS, BARRA2, …), select CFS by name:

```yaml
FORCING_DATASET: CFS
DATA_ACCESS: community
ALLOW_UNGATED_BACKENDS: true   # 'CFS' carries no parity grade — explicit opt-in

# Required: a CFS product id ("provider:product", as `cfs fetch -P` takes),
# or a bare provider slug when the provider offers exactly one product.
CFS_PRODUCT: gefs:atmos_0p25

# Optional: comma-separated canonical variable names (default: all the
# product offers), and provider-specific connector configuration.
CFS_VARIABLES: air_temperature, precipitation_flux
CFS_CONNECTOR_CONFIG:
  members: [gec00]
```

The bounding box and time range come from the standard SYMFLUENCE domain
keys. Embedders driving the protocol directly can pass
`options={'product': 'gefs:atmos_0p25', 'connector_config': {...}}` on the
`AcquisitionRequest` instead of config keys.

## What happens downstream

```
cfs.fetch_sync()                     SYMFLUENCE
canonical-v1 xr.Dataset       ──▶    raw_data/*.nc + acquisition_manifest.json   (CommunityForcingBackend)
                              ──▶    CFIF rename + wind_speed + attrs            (CanonicalV1Handler, schema-dispatched)
                              ──▶    EASYMORE HRU remap                          (SYMFLUENCE resampling)
                              ──▶    model-ready forcing                         (SUMMA, FUSE, …)
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
  print('backend:', R.acquisition_backends.get('community')); \
  print('handler:', R.dataset_handlers.get('canonical-v1'))"
```
