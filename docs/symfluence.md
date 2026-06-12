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

**Spatial domains:** regional datasets (CARRA Arctic-only, CERRA Europe-only,
HRRR CONUS-only, Daymet North-America-only) refuse an out-of-domain bbox at
`acquire()` time with a plain `AcquisitionError` naming the domain. This is
deliberately *not* a decline-and-fallback: the limit is a property of the
dataset itself — no backend can serve CARRA south of the Arctic — so failing
loudly beats a doomed native retry. (The `DatasetCapability` contract has no
spatial field yet; when it grows one, this check moves to selection time.)

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
| `CASR` | `rdrs:casr_v32` | **projected** (rotated pole) | `bit-identical` — alias of the RDRS capability (same ECCC CaSR family / same PAVICS store, see fine print) |
| `CONUS404` | `conus404:hourly` | **projected** (LCC 4 km) | `value-identical:1ulp` (exp13: T/q/p/u/v + wind_speed bitwise; precip + radiation ≤ 1 float32 ulp; first radiation step differs by design, see fine print) |
| `NWM3_RETROSPECTIVE` | `aorc_nwm:conus_1km` | **projected** (LCC 1 km) | `bit-identical` (exp15: all 8 variables + 2-D lat/lon + time bitwise; precip convention differs — flux vs ×3600 accumulation, value-equivalent) |
| `CARRA` | `carra:single_levels` | regular lat/lon (CDS-interpolated) | `value-identical:grib-repack` (exp11: time bitwise; every field differs only by CDS's per-request GRIB re-packing + the documented q-epsilon derivation, see fine print). **Arctic-only** (≥ 55°N); `CARRA_DOMAIN` selects the west/east CDS split |
| `CERRA` | `cerra:single_levels` | regular lat/lon (CDS-interpolated) | `value-identical:grib-repack` (exp12: pressure + time bitwise, rest grib-repack/q-epsilon; **longwave is community-only** — the native handler requests a CDS variable name CERRA doesn't have, see fine print). **Europe-only**; archive ends 2021-06-30 |
| `HRRR` | `hrrr:sfc_anl` | **projected** (LCC 3 km) | `bit-identical` (exp14: all 7 analysis variables + time bitwise; 2-D lat/lon ≤ 3.9 × 10⁻⁶ ° — native recomputes them with pyproj, CFS reads the published grid arrays). **No precipitation** in the analysis stream (either side). **CONUS-only** |
| `DAYMET` | `daymet:daily_v4` | **projected** (daily LCC 1 km) | `bit-identical:point-sampled` (exp16: all four canonical derivations bitwise vs the raw values from the native single-pixel route at every sampled cell; full-grid comparison blocked by a NASA Hyrax outage on campaign day, see fine print). Daily noon-anchored. **North-America-only** |
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

**CASR fine print:** SYMFLUENCE's `CASR` is the same ECCC CaSR product family
as RDRS. Natively it is MAF/datatool-only (HPC-prestaged **CaSR v3.1**
extracts with RPN variable names like `CaSR_v3.1_P_TT_1.5m`; `casr_utils`
converts the non-SI units heuristically). The only public cloud upstream is
the PAVICS **CaSR v3.2** store — exactly what `rdrs:casr_v32` reads, verified
bitwise against the native `RDRSAcquirer` in exp10 (and `casr_utils`
explicitly supports that consolidated v3.2 layout too). The community
backend therefore serves CASR as v3.2; the v3.1 HPC staging remains
native-only by definition.

**CONUS404 fine print:** both sides read the HyTEST OSN Zarr. The two
radiation fields are stored as running accumulations (J m⁻²): the community
pipeline de-accumulates them against a real pre-window hour, while the native
preprocessing back-fills the first step from step 2 — so the *first timestep
of a fetch* differs (community is the physically correct increment). All
later steps agree to ≤ 1 float32 ulp (`/3600` vs `*(1/3600)` op order, same
as precipitation).

**CARRA/CERRA fine print (the `grib-repack` grade):** both sides submit CDS
requests against the same datasets with the same server-side `grid`
interpolation, but the native handler pads the area ±0.1° (CARRA also uses
0–360 longitudes) while CFS requests the exact bbox. CDS/MARS re-encodes the
GRIB **per request**, so the 16-bit simple-packing reference/scale are
computed over different field min/max and the decoded float32 values sit on
*offset quantization lattices* (verified: air temperature on a 2⁻¹⁵ K comb
and pressure on a 2⁻⁷ Pa comb on both sides, different anchors). Differences
are bounded by a few packing quanta — T ≤ 2.4 × 10⁻⁴ K, p ≤ 0.18 Pa, fluxes
≤ 1.6 × 10⁻⁴ relative — and CERRA's pressure came out bitwise (same field
extremes in both areas). On top of that, `specific_humidity` carries the
documented derivation difference (native ε = 0.622 with `P − 0.378e`; CFS
ε = 0.62198 with `P − (1−ε)e`): ≤ 5.5 × 10⁻⁵ relative. Nothing else differs.

**CERRA longwave caveat:** the CERRA CDS form names downwelling longwave
`surface_thermal_radiation_downwards` (ERA5-style), while CARRA names it
`thermal_surface_radiation_downwards`. The native SYMFLUENCE handler requests
the CARRA-style name for **both** datasets; CDS **silently drops** the unknown
name (live request `99dc24ae…` returned only `tp`+`ssrd`), after which the
native handler's required-variable validation hard-fails — i.e. native CERRA
acquisition cannot complete at all on the validated branch. CFS requested the
same wrong name until this campaign caught it (`connectors/cerra.py` fixed,
live-verified); community CERRA therefore delivers all 7 variables, with
longwave necessarily ungraded against a native reference (it is produced by
the same fixed request/decode path as the six graded variables).

**HRRR fine print:** both sides read the same hrrrzarr float16 chunks
(upcast to float32): all 7 analysis variables and the time axis are bitwise
identical. The 2-D lat/lon coordinates differ by ≤ 3.9 × 10⁻⁶ ° (~0.4 m)
because the native handler *recomputes* them with a pyproj LCC transform
while CFS reads the archive's published `grid/HRRR_chunk_index.zarr` arrays.
Campaign finding on the native side: its bbox windowing no-ops (the hrrrzarr
variable groups carry no latitude coordinate to mask on), so the native
handler downloads the full CONUS grid (~1.3 GB/day; 42 min for the 1-day
experiment vs 96 s for the windowed community fetch).

**DAYMET fine print:** the `point-sampled` qualifier is deliberate honesty:
on campaign day NASA Hyrax answered every OPeNDAP data request with 120-s
read timeouts and intermittent 404/500s, so the full-grid raw comparison
could not complete. The banked evidence: all four canonical derivations
(`T=(tmax+tmin)/2+273.15`, `precip=prcp/86400`, `SW=srad·dayl/86400`,
`dewpoint=inverse-Bolton(vp)`) recomputed in float32 from the raw daily
values served by ORNL's **single-pixel API** — the native handler's own
point route, healthy and independent of Hyrax — are bitwise identical to the
community canonical artifact at every sampled cell (5 cells × 14 days), and
the API-reported containing-cell LCC x/y matches the canonical store's cell
coordinates to ≤ 0.3 m. Native-side findings: the native *gridded* OPeNDAP
route slices the descending Daymet `y` axis with an ascending slice and so
returns empty subsets for every variable — it cannot produce gridded data at
all on the validated branch — and its OPeNDAP URL is `https://`-hardcoded
(fails under libnetcdf ≥ 4.10 probing); there is no THREDDS-NCSS fallback
(ORNL's legacy THREDDS endpoint now 404s into the same DMR++ backend).

**Excluded:** `MSWEP` and `EM-EARTH` are *not claimed* until live
native-vs-community parity validation is possible (blocked: no rclone Google
Drive remote for MSWEP; the EM-Earth S3 bucket denies anonymous GET and the
*native* acquirer is S3-only — credentialed via `EM_EARTH_S3_ANON: false`
but with no FRDR route or local-staging mode — so there is nothing
native-side to compare against without AWS credentials).
Their native handlers keep running untouched under every `DATA_ACCESS` value.
Note the *CFS-side* EM-Earth blocker is gone: the connector now has an
anonymous FRDR HTTPS source and `data_dir` staging, with units file-verified
and the canonical derivations validated bitwise against raw FRDR values
(exp17) — see the [catalog notes](catalog.md).

## Per-dataset opt-out

A flat `<NATIVE_NAME>_BACKEND` key overrides the global gate per dataset:

```yaml
DATA_ACCESS: community     # community everywhere it's covered...
ERA5_BACKEND: native       # ...but keep native ERA5 acquisition
```

## Projected grids

Three projected grid families are served, all through the same
`CanonicalV1Handler` pathway. The canonical-v1 layout keeps the native index
dims with 2-D `latitude`/`longitude` auxiliary coordinates (see the
[canonical-v1 spec](canonical-v1.md)):

- **rotated pole** — `rlat`/`rlon` dims (RDRS / CASR, CaSR v3.2);
- **Lambert conformal conic** — `y`/`x` dims in projected metres
  (CONUS404 4 km, NWM3 retrospective 1 km, HRRR 3 km);
- **daily LCC** — same `y`/`x` + 2-D lat/lon structure but a *daily* time
  axis anchored at noon (Daymet 1 km).

`CanonicalV1Handler`:

- reports coordinate names `('latitude', 'longitude')` (EASYMORE handles 1-D
  and 2-D coords by name); the projected layout is detected from the 2-D
  latitude coordinate, never from dim names,
- splits the consolidated canonical store into native-pipeline-style monthly
  files (`{DATASET}_monthly_YYYYMM.nc`). Hourly stores get the exact native
  behaviour (complete full-month hourly axis, gap-filled like the native
  consolidated path); non-hourly stores are rebuilt at their **native step**
  (inferred as the median time diff), anchored on the store's own
  timestamps — a daily Daymet store stays daily and keeps its noon stamps,
- builds the forcing-grid shapefile with one polygon per native cell from the
  2-D coordinate corners — ported from the proven native RDRS implementation
  (the grids were verified bitwise identical in exp10/exp13/exp15, so the
  geometry matches) and identical for every projected family.

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
