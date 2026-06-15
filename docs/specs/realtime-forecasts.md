# Spec: real-time forecast forcing products

Status: **draft / design** (not yet implemented). Owner: CFS maintainers.
Scope set on 2026-06-15 to: **NWM forecast forcing**, **NOAA short-range CONUS**
(HRRR-forecast, RAP, NAM), and **global non-NOAA** (ECMWF IFS/AIFS open-data,
NBM). ECCC Canadian models (HRDPS/RDPS/GDPS/GEPS) were considered and
**deferred** — they need a new MSC Datamart/GeoMet protocol and are tracked
separately.

CFS already ships two forecast connectors — `gfs` (deterministic, 0.25° global)
and `gefs` (ensemble, `member` dim) — both built on the Herbie pattern: read the
GRIB2 `.idx` sidecar, HTTP byte-range fetch only the surface messages, decode
with cfgrib (the `forecast` extra). Every product below extends that established
contract. CFS's boundary is unchanged: acquire → subset to bbox+time →
harmonize to canonical-v1 → lazy `xarray.Dataset`. No HRU remap, no model schema.

---

## 0. Cross-cutting prerequisite — extract a shared forecast/GRIB-idx helper

The `.idx` parse, HTTP byte-range fetch, single-message cfgrib decode, and
`(grib_var, grib_level) → byte range` lookup are **duplicated verbatim** in
`connectors/gfs.py` and `connectors/gefs.py` (`_parse_idx`, `_http_range`,
`_open_message`, `_byte_range`, `_read_field`). Adding three-plus more GRIB
forecast connectors against the same NOAA `.idx` format would copy it five more
times.

**Do this first:** extract `cfs/connectors/protocols/grib_idx.py` exposing:

- `parse_idx(text) -> list[(grib_var, grib_level, start_byte)]` (NOAA `.idx`
  colon format)
- `http_range(url, start, end) -> bytes`
- `open_message(raw, internal) -> xr.Dataset` (decode one message, rename to
  `internal`, drop GRIB scalar coords, keep `latitude`/`longitude`)
- `byte_range(idx, grib_var, grib_level)` and `read_field(...)`
- the cycle/lead helpers in a small `ForecastCycle` mixin: `cycle_for(start,
  step_h)`, `lead_available(lead, hourly_max, max_lead, step)`,
  valid-time→lead-hour mapping, and the bucket de-accumulation (`acc`:
  `cur-prev`; `ave`: `2*cur-prev`) lifted from `gefs.py`.

Then refactor `gfs`/`gefs` onto it (behaviour-preserving; their live-verified
parity must not move) before adding the new connectors. This is the single most
load-bearing piece of work here — every NOAA GRIB connector below assumes it.

> **Note on ECMWF:** ECMWF open-data does **not** use the NOAA `.idx` colon
> format — it ships a `.index` sidecar with one JSON object per message. Keep the
> ECMWF index parser separate (a sibling function in `grib_idx.py` or its own
> module); only the byte-range fetch + cfgrib decode are shared.

---

## Tier A — NWM forecast forcing (lowest effort: un-defer existing config)

**`nwm_operational.py` already contains this.** Its `CONFIG` map and docstring
define `short_range` and `medium_range`; only `analysis_assim` is currently
exposed (the forecast configs were deliberately deferred). The S3 path pattern,
the NWM v3.0 1km LCC grid generation (`subset_2d_grid`, shared with `aorc_nwm`),
and the LDASIN→canonical mapping are **all already implemented**. This is an
un-deferral, not a new connector.

| | |
|---|---|
| **Source** | `s3://noaa-nwm-pds/nwm.YYYYMMDD/{config}/nwm.t{cyc}z.{config}.forcing.f{FFF}.conus.nc` (anonymous; NetCDF, **not** GRIB) |
| **Products** | `nwm_operational:short_range`, `:medium_range` (and optionally `:long_range`) |
| **Grid** | NWM v3.0 1km LCC CONUS — lat/lon generated from projection params (already coded) |
| **Cadence / leads** | short_range: hourly cycles, f001–f018. medium_range: 00/06/12/18Z, f001–f240 (3-hourly past f024); 7 ensemble members (`mem1..7`) → reuse the `member` dim, or expose mem1 only in v1. long_range: 4 members, f000–f720 3-hourly |
| **Canonical vars** | RAINRATE→`precipitation_flux`, T2D→`air_temperature`, Q2D→`specific_humidity`, U2D→`eastward_wind`, V2D→`northward_wind`, LWDOWN→`surface_downwelling_longwave_flux`, SWDOWN→`surface_downwelling_shortwave_flux`, PSFC→`surface_air_pressure` — **identical mapping to the existing analysis_assim path** |
| **Accumulation** | RAINRATE is already a flux (mm/s → kg m⁻² s⁻¹); no de-accumulation. Identity SI elsewhere |
| **Auth** | anonymous |
| **Archive window** | rolling — `noaa-nwm-pds` keeps roughly the last ~30–48h of cycles (VERIFY current retention); NOMADS ~2 days. No deep history |
| **Reuse** | ~100% of the machinery already exists; mainly lift the deferral, add the `f{FFF}` lead loop + cycle selection, and the medium/long-range member dim |
| **Effort** | **S** — smallest item; mostly wiring + tests |
| **Verify** | exact forecast filename (`...forcing.fFFF...` vs `...forcing.conus.fFFF...`), member directory naming, current retention window |

---

## Tier B — NOAA short-range CONUS (GRIB2 byte-range, reuses §0 helper)

All three are anonymous NOAA Open Data S3 buckets with `.idx` sidecars, decoded
exactly like `gfs`. The new dimension vs `gfs` is **2-D projected (LCC) grids**
— but cfgrib attaches `latitude`/`longitude` 2-D coords on decode, so
`grid2d.bbox_index_window` / `subset_2d_grid` handle the windowing (same path as
the existing `hrrr` and `aorc_nwm` connectors). Precip on these models is a
GRIB **accumulation** that resets, so reset-aware de-accumulation
(`cfs.subset.deaccumulate`) applies — unlike `gfs`'s instantaneous PRATE.

### B1. HRRR-forecast — `hrrr:sfc_fcst` (highest leverage)

The US short-range hydrology standard; complements the existing analysis-only
`hrrr:sfc_anl` (which reads hrrrzarr). The **forecast** stream is GRIB2, so it's
a new code path on the `hrrr` connector rather than a hrrrzarr extension.

| | |
|---|---|
| **Source** | `s3://noaa-hrrr-bdp-pds/hrrr.YYYYMMDD/conus/hrrr.t{cyc}z.wrfsfcf{FF}.grib2` (+ `.idx`) |
| **Grid** | 3km LCC CONUS, 2-D lat/lon from cfgrib |
| **Cadence / leads** | hourly cycles; f00–f18 every cycle, **f00–f48 for the 00/06/12/18Z runs**. Sub-hourly (15-min) available in `wrfsubhf` — out of scope for v1 |
| **Canonical vars** | TMP 2m→`air_temperature`, SPFH 2m→`specific_humidity`, PRES surface→`surface_air_pressure`, UGRD/VGRD 10m→winds, DSWRF/DLWRF→radiation, **APCP surface→`precipitation_flux`** |
| **Accumulation** | APCP is accumulated; HRRR resets the bucket per cycle — de-accumulate `[lead-1, lead]` then ÷3600 to flux. DSWRF/DLWRF are instantaneous in HRRR (VERIFY: some HRRR builds ship averaged radiation) |
| **Auth** | anonymous |
| **Archive window** | long history on `noaa-hrrr-bdp-pds` (2014→present) |
| **Reuse** | §0 helper + `grid2d` + `deaccumulate`; the spatial domain guard already exists for `hrrr` |
| **Effort** | **M** |

### B2. RAP — `rap:awp130_fcst`

Hourly-updating 13km CONUS; the coarser, longer-history companion to HRRR.

| | |
|---|---|
| **Source** | `s3://noaa-rap-pds/rap.YYYYMMDD/rap.t{cyc}z.awp130pgrbf{FF}.grib2` (+ `.idx`); `awip32` = 32km North America |
| **Grid** | 13km LCC CONUS (awp130) |
| **Cadence / leads** | hourly cycles; f00–f21, extended to f51 on the 03/09/15/21Z runs (VERIFY current lead structure) |
| **Canonical vars** | same surface set as HRRR (TMP/SPFH/PRES/UGRD/VGRD/DSWRF/DLWRF/APCP) |
| **Accumulation** | APCP accumulation → de-accumulate → flux |
| **Auth** | anonymous |
| **Reuse** | identical to B1 modulo grid params and lead table |
| **Effort** | **M** (≈B1) |

### B3. NAM — `nam:awphys_fcst` (+ optional `nam:conusnest_fcst`)

12km North America; the 3km CONUS nest is a higher-res option.

| | |
|---|---|
| **Source** | `s3://noaa-nam-pds/nam.YYYYMMDD/nam.t{cyc}z.awphys{FF}.tm00.grib2` (12km); nest: `nam.t{cyc}z.conusnest.hiresf{FF}.tm00.grib2` (3km) |
| **Grid** | 12km LCC NA (awphys) / 3km LCC CONUS (nest) |
| **Cadence / leads** | 00/06/12/18Z; hourly to f36 then 3-hourly to f84 (12km); nest hourly to f60 |
| **Canonical vars** | same surface set |
| **Accumulation** | APCP — note NAM's accumulation reference period varies by lead (per-hour vs 3-hour buckets); de-accumulation must read the GRIB time-range indicator, not assume hourly. **This is the main NAM-specific risk** |
| **Auth** | anonymous |
| **Effort** | **M–L** (the variable accumulation window adds care) |

---

## Tier C — global non-NOAA

### C1. ECMWF IFS + AIFS open-data — `ecmwf_open:ifs_0p25`, `ecmwf_open:aifs_0p25`

The major free, real-time, global forecast outside NOAA. IFS is the physics
model; AIFS is ECMWF's ML model. Both 0.25° global on a regular lat/lon grid.

| | |
|---|---|
| **Source** | `https://data.ecmwf.int/forecasts/YYYYMMDD/{cyc}z/ifs/0p25/oper/...grib2` (also AWS `ecmwf-forecasts` bucket); AIFS at `.../aifs-single/0p25/oper/...` |
| **Index** | `.index` sidecar — **one JSON object per message, NOT the NOAA `.idx` colon format**. Needs its own parser (see §0 note). Byte-range fetch + cfgrib decode are still shared |
| **Grid** | 0.25° regular lat/lon, global |
| **Cadence / leads** | oper (HRES) 00/12Z to 240h + 06/18Z to 90h; steps 3-hourly to 144h then 6-hourly. AIFS 00/06/12/18Z, 6-hourly to 360h. ENS (`enfo`) is a future ensemble extension with a `member` dim |
| **Canonical vars** | 2t→`air_temperature`, **2d (dewpoint)→derive `specific_humidity`** via `cfs.derive.humidity` (open-data ships dewpoint, not q — same derivation path CARRA/CERRA use), 10u/10v→winds, sp→`surface_air_pressure`, tp→`precipitation_flux` |
| **Accumulation** | tp is accumulated from forecast start → de-accumulate → flux |
| **⚠ Radiation gap** | **VERIFY whether `ssrd`/`strd` are in the free open-data field set.** The open-data catalog is a *subset* of operational fields; if radiation is absent, this product cannot supply `surface_downwelling_shortwave/longwave_flux` and must declare a reduced canonical var list (document the gap honestly, like other partial-coverage connectors) |
| **Auth** | anonymous |
| **License** | ECMWF open-data is CC-BY-4.0 — different from NOAA public-domain; record in the catalog |
| **Effort** | **L** (new index format + dewpoint derivation + radiation verification) |

### C2. NBM — `nbm:co_fcst` (lower priority; caveat-heavy)

NOAA's National Blend of Models — a statistical blend, 2.5km CONUS. Useful for
T/wind, but precipitation is **probabilistic** (percentiles / PoP), not a clean
deterministic accumulation, so it's a poorer fit for deterministic hydro forcing
than B1–B3.

| | |
|---|---|
| **Source** | `s3://noaa-nbm-grib2-pds/blend.YYYYMMDD/{cyc}/core/blend.t{cyc}z.core.f{FFF}.co.grib2` (+ `.idx`) |
| **Grid** | 2.5km LCC CONUS |
| **Cadence / leads** | hourly cycles; hourly to f36, then 3/6-hourly to f264 (VERIFY) |
| **Canonical vars** | TMP/winds map cleanly; **APCP is interval-probabilistic** — for v1 expose the deterministic/mean fields only and document that NBM precip is not a physical accumulation |
| **Auth** | anonymous |
| **Effort** | **M**, but **recommend deferring** until A/B land — the precip semantics make it the weakest forcing fit of the set |

---

## Recommended implementation order

1. **§0 shared `grib_idx` helper** + refactor `gfs`/`gefs` onto it (unblocks
   everything; must preserve their live-verified parity).
2. **Tier A — NWM forecast forcing** (un-defer `short_range`/`medium_range`).
   Smallest, highest-confidence, all infra exists.
3. **B1 — HRRR-forecast.** Highest single-product leverage for US hydrology.
4. **B2 — RAP**, then **B3 — NAM** (NAM last in the tier for its accumulation
   quirk).
5. **C1 — ECMWF open-data** (IFS first, then AIFS) — biggest new surface
   (non-NOAA index format, license, radiation gap), so it gets its own cycle.
6. **C2 — NBM** only if there's demand; document the probabilistic-precip caveat.

## Open questions to settle by live probe before coding each connector

- NWM: exact forecast filename, member directory naming, current S3 retention.
- HRRR/RAP: are DSWRF/DLWRF instantaneous or averaged in the forecast stream?
- NAM: confirm the APCP accumulation reference period per lead band.
- ECMWF open-data: **is radiation (ssrd/strd) in the free field set?** Confirm
  the `.index` JSON schema and the AWS-bucket vs `data.ecmwf.int` parity.
- All: confirm `.idx`/`.index` sidecars exist for every targeted product+lead
  (a few NOAA products historically lacked idx for some cycles).

## Catalog / inventory follow-ups

- Add stubs to the (currently empty) "Migration backlog" block in
  `inventory/providers.yaml` for each accepted product, with `verified: false`
  until live-probed.
- Each forecast product declares `temporal_resolution`, the rolling-archive
  caveat, and (for NWM medium/long range and any ENS) the `member` variant knob,
  mirroring `gefs`.
