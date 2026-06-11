# CLI

The `cfs` command is installed with the package
(`pip install community-forcing-service`).

```bash
cfs --help
cfs --version
```

## Commands

| Command | Purpose |
| --- | --- |
| `cfs providers` | List registered provider slugs |
| `cfs products` | List forcing products + their canonical variables (optionally `-p <provider>`) |
| `cfs fetch` | Acquire + subset a product to a canonical gridded dataset |

## providers

```bash
cfs providers
```

Prints one provider slug per line (`era5_arco`, `aorc`, `hrrr`, …). These are
the slugs used as the prefix of product IDs and accepted by
`get_connector()` in the [Python API](python-api.md).

## products

```bash
cfs products                 # every product from every provider
cfs products -p era5_arco    # one provider
```

Each line shows the product ID (`{provider}:{product}`), native resolution,
temporal resolution, and the canonical variables the product can deliver:

```
era5_arco:single_levels  [0.25°, hourly]  air_temperature, dewpoint_temperature, ...
```

Note that `cfs products` queries each provider's catalog metadata; providers
whose optional dependencies are not installed are skipped with a warning.

## fetch

```bash
cfs fetch \
  -P era5_arco:single_levels \
  -b -114.5,50.7,-114.0,51.1 \
  --start 2015-06-01T00:00 --end 2015-06-01T06:00 \
  -v air_temperature,precipitation_flux \
  -o forcing.nc
```

| Option | Meaning |
| --- | --- |
| `-P, --product` | Product ID, e.g. `era5_arco:single_levels` (required) |
| `-b, --bbox` | `min_lon,min_lat,max_lon,max_lat` in EPSG:4326 (required) |
| `--start`, `--end` | Time range, ISO 8601 (required) |
| `-v, --variables` | Comma-separated [canonical variable names](canonical-v1.md#canonical-variables); default: all the product offers |
| `-o, --output` | Also write the canonical cube to this NetCDF path |
| `--load/--lazy` | Materialize the cube before reporting (default: lazy) |

`cfs fetch` prints the `FetchResult` as JSON — provenance, shape
(`n_times`/`n_lat`/`n_lon`), elapsed time, and any QC `warnings`:

```json
{
  "product_id": "era5_arco:single_levels",
  "provider": "era5_arco",
  "variables": ["air_temperature", "precipitation_flux"],
  "n_times": 7,
  "n_lat": 2,
  "n_lon": 3,
  "lazy": true,
  "provenance": "ARCO-ERA5 GCS Zarr; canonical-v1",
  "warnings": []
}
```

With `-o`, the dataset is materialized and written as NetCDF. This is a
convenience for inspection; writing model-specific forcing schemas is
deliberately out of scope (see [Home](index.md)).

## Guardrail environment variables

| Variable | Effect |
| --- | --- |
| `CFS_MAX_AREA_DEG2` | Refuse bboxes larger than this area (deg²) |
| `CFS_MAX_CELLS_PER_FETCH` | Refuse fetches whose time × lat × lon cell count exceeds this |
| `CFS_QC_ENABLED` | Toggle the advisory range QC (default on) |
| `CFS_FETCH_CONCURRENCY` | Concurrency for per-file stores (one OPeNDAP/NetCDF open per hour/day/year) |
