# Canonical Schema — `canonical-v1`

This page is the **normative specification** of the dataset every CFS
connector returns. It is the contract downstream frameworks code against: if
your code consumes the output of `fetch()`, this page defines what you may
rely on. The implementation lives in `cfs.core.vocabulary` (the vocabulary)
and `cfs.subset.canonical.harmonize` (the transformation); the vocabulary
module is the single source of truth if this page and the code ever disagree.

"MUST" describes what CFS guarantees about its output; "MUST NOT (rely on)"
describes what consumers cannot assume.

## Dataset identification

Every canonical dataset carries the global attribute:

| Attribute | Value |
| --- | --- |
| `cfs_schema` | `"canonical-v1"` (literal string) |

Consumers SHOULD check this attribute to confirm they hold a canonical
dataset. Connectors MAY add further provenance attributes (e.g. the CMIP6
connectors set `cmip6_model` / `cmip6_scenario` / `cmip6_member`); consumers
MUST ignore global attributes they do not recognize.

## Canonical variables

Data variables are named by the `CanonicalVar` enum — CF-aligned standard
names — and are **always** in the canonical units below. A dataset contains
the subset of these variables that was requested and that the product offers,
never variables outside this vocabulary, and never duplicated information
(e.g. a derivation input such as relative humidity is consumed, not emitted).

| Canonical name | Units | Description | Valid range (QC) |
| --- | --- | --- | --- |
| `air_temperature` | `K` | Near-surface (2 m) air temperature | 180 – 340 |
| `dewpoint_temperature` | `K` | Near-surface (2 m) dewpoint temperature | 180 – 320 |
| `specific_humidity` | `kg kg-1` | Near-surface specific humidity | 0 – 0.1 |
| `precipitation_flux` | `kg m-2 s-1` | Precipitation rate (rain + snow water equivalent) | 0 – 0.1 |
| `eastward_wind` | `m s-1` | Eastward (u) wind component at 10 m | −150 – 150 |
| `northward_wind` | `m s-1` | Northward (v) wind component at 10 m | −150 – 150 |
| `wind_speed` | `m s-1` | Scalar wind speed at 10 m (only when the product lacks u/v) | 0 – 150 |
| `surface_air_pressure` | `Pa` | Surface air pressure | 40 000 – 110 000 |
| `surface_downwelling_shortwave_flux` | `W m-2` | Surface downwelling shortwave radiation | 0 – 1500 |
| `surface_downwelling_longwave_flux` | `W m-2` | Surface downwelling longwave radiation | 0 – 750 |

The valid ranges are **advisory** QC bounds (`CanonicalSpec.valid_range`):
sampled values outside them produce `FetchResult.warnings`, never masking or
failure. Consumers MUST NOT assume data have been clipped to these ranges
(NARR, for instance, carries occasional tiny negative precipitation from its
source fields, flagged by QC).

Wind is delivered as `eastward_wind` + `northward_wind` when the product
publishes components, and as `wind_speed` only when the product publishes a
scalar speed (GLDAS, FLDAS). A consumer needing speed MUST handle both cases.

## Variable attributes

Every canonical data variable carries exactly these attributes (plus, where
set, the optional one):

| Attribute | Content |
| --- | --- |
| `standard_name` | The canonical name itself (CF-aligned), e.g. `"air_temperature"` |
| `units` | The canonical units string from the table above |
| `long_name` | Human-readable description |
| `cfs_source_name` | The provider's native variable name this was derived from (provenance) |
| `cfs_conversion` *(optional)* | Note describing a non-trivial conversion (e.g. de-accumulation) |

The transformation from native data is, in order: optional reset-aware
de-accumulation (for running-total fields), then the linear map
`canonical = source × scale + offset` to canonical units.

## Fluxes, never accumulations

**Normative rule:** `precipitation_flux`,
`surface_downwelling_shortwave_flux`, and
`surface_downwelling_longwave_flux` are always **rates** (`kg m-2 s-1`,
`W m-2`), valid over the source's native time step — never accumulated
quantities. Whatever the provider ships (ERA5-Land running totals that reset
daily, GEFS 6-hour buckets, interval means stamped mid-interval), CFS
de-accumulates and converts before returning. A consumer MUST NOT apply its
own de-accumulation to canonical data.

## Dimensions and coordinates

The time coordinate is always named `time` (a datetime64 axis, UTC). The
spatial layout depends on the product's grid, and **consumers must branch on
it**:

### Regular latitude/longitude grids

Most products (see the [catalog](catalog.md) "grid" column) are on regular
grids:

- **Dims:** `time`, `latitude`, `longitude`
- **Coords:** 1-D `latitude` (degrees north, ascending) and 1-D `longitude`
  (degrees east), each indexing its own dimension.

Longitude values retain the provider's native convention — most stores use
[−180, 180], but some (e.g. BARRA2) publish [0, 360). Requests are always
made in [−180, 180]; CFS handles the translation (and antimeridian-crossing
boxes) during subsetting but does not re-normalize the returned coordinate
values.

### Projected / curvilinear grids

Products on rotated-pole or Lambert-conformal grids (`rdrs`, `conus404`,
`hrrr`, `daymet`, `narr`, `aorc_nwm`, `nwm_operational`) keep their **native
index dimensions** — `rlat`/`rlon` (rotated pole) or `y`/`x` (LCC) — because
interpolating to a regular grid would degrade exactly the high-resolution
information these products exist for:

- **Dims:** `time`, plus the native pair (`rlat`/`rlon` or `y`/`x`)
- **Coords:** 2-D `latitude` and `longitude` auxiliary coordinates over that
  native pair, giving the geographic position of every cell in degrees,
  longitudes in [−180, 180].

The subset is the smallest contiguous index window (plus a small buffer)
covering the requested bbox, so a projected-grid subset contains some cells
*outside* the bbox; consumers needing an exact cut must mask on the 2-D
lat/lon coordinates.

Dispatch pattern:

```python
if ds.latitude.ndim == 1:          # regular grid
    weights = area_weights(ds.latitude)
else:                              # projected grid: 2-D lat/lon over native dims
    ydim, xdim = ds.latitude.dims
    mask = within_polygon(ds.latitude, ds.longitude, hru_geometry)
```

`FetchResult.n_lat` / `n_lon` report the native index dims in both cases.

### Extra dimensions

Specific products add documented dimensions, always in addition to (never
replacing) the above:

- `gefs` — a `member` dimension (ensemble member labels `gec00`,
  `gep01` … `gep30`).
- `na_cordex` — a `member_id` dimension (the CORDEX multi-model ensemble).

Consumers MUST tolerate (select-from or reduce) extra dimensions on products
documented to have them.

## Time conventions

- The `time` coordinate is UTC, encoded as `datetime64` values; CFS never
  returns provider-local time.
- Timestamps are the provider's native stamps over the requested range,
  inclusive of both endpoints where the store has data; CFS does not
  resample, interpolate, or fill gaps.
- **Instantaneous vs interval semantics are provider-native.** State
  variables (temperature, pressure, wind, humidity) are instantaneous values
  at the stamp. Flux variables are rates representative of the source's
  native step ending at (or centred on, per the provider's convention) the
  stamp; where a provider stamps interval means off the shared axis (BARRA2's
  half-hour midpoints), CFS aligns them to the instantaneous axis and records
  the choice in the connector. Forecast products (`gfs`, `gefs`) lack flux
  values at lead 0 (analysis time), so a range starting exactly at a cycle
  time has its first flux stamp one step later.

## Stability

`canonical-v1` is append-only: new canonical variables may be added to the
vocabulary, but the names, units, attribute keys, grid layouts, and the
fluxes-never-accumulations rule above will not change meaning within v1. A
breaking revision would ship as `cfs_schema = "canonical-v2"`.
