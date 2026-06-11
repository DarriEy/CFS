# Python API

CFS is used in-process — there is no server. Everything you need is exported
from the top-level `cfs` package (see `cfs.__all__`).

## One-shot fetch (start here)

`cfs.fetch_sync()` runs the whole pipeline — registry discovery, connector
resolution, acquire + subset + harmonize — in a single call:

```python
import cfs

ds, result = cfs.fetch_sync(
    "era5_arco:single_levels",                       # product ID: {provider}:{product}
    bbox=(-114.5, 50.7, -114.0, 51.1),               # min_lon, min_lat, max_lon, max_lat
    time_range=("2015-06-01T00:00", "2015-06-01T06:00"),
    variables=["air_temperature", "precipitation_flux"],  # None = all the product offers
)
```

From async code, `await` the underlying coroutine directly (`fetch_sync`
raises a clear error if called inside a running event loop):

```python
ds, result = await cfs.fetch(
    "era5_arco:single_levels",
    bbox=(-114.5, 50.7, -114.0, 51.1),
    time_range=("2015-06-01T00:00", "2015-06-01T06:00"),
)
```

Both accept:

- **Identifier** — a full product ID (`"era5_arco:single_levels"`, the same
  string `cfs fetch -P` takes; the provider slug is the part before the first
  `:`), or a **bare provider slug** (`"aorc"`), which resolves automatically
  when that provider offers exactly one product (otherwise a `ValueError`
  lists the candidate product IDs).
- **bbox / time_range** — typed models (`cfs.BoundingBox`, `cfs.TimeRange`)
  or plain tuples (floats; `datetime`s or ISO-8601 strings).
- **variables** — `cfs.CanonicalVar` members or their string values.
- **config** — an optional provider-specific connector config dict (see
  [below](#connector-configuration-config-dict-injection)), e.g.
  `cfs.fetch_sync("gefs:ensemble_0p25", ..., config={"members": ["gec00"]})`.

The returned dataset follows the [canonical-v1](canonical-v1.md) contract and
is **lazy** (dask-backed) where the protocol allows; `result` is the
[FetchResult](#fetchresult) provenance/shape metadata.

## Runtime configuration: `cfs.configure()`

CFS settings are env-driven (`CFS_*` variables — timeouts, fetch guardrails,
cache directory, concurrency) and cached on first read. Embedders that can't
set environment variables before import can override them programmatically:

```python
import cfs

cfs.configure(
    cache_dir="/scratch/cfs-cache",   # where whole-file HTTP sources cache downloads
    provider_timeout_s=300,
    max_area_deg2=900,
)
```

Each keyword must be a `Settings` field (`cfs.core.config.Settings`); the
value is written to the corresponding `CFS_<FIELD>` environment variable and
the settings cache is cleared, so the override takes effect everywhere
(connectors read settings at call time, never at import time). Pass `None` to
drop an override and fall back to the environment/default. The new effective
`Settings` is returned.

## Advanced: the discover / get_connector / fetch pattern

The facade wraps a small lower-level seam, which remains fully public — use it
to hold a connector open across multiple fetches, list products
programmatically, or control the lifecycle yourself.

```python
from datetime import datetime

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar

discover()                            # import all connector modules (registers them)
Conn = get_connector("era5_arco")     # connector *class* for a provider slug

async with Conn() as conn:
    ds, result = await conn.fetch(
        "era5_arco:single_levels",                    # product ID: {provider}:{product}
        BoundingBox(min_lon=-114.5, min_lat=50.7, max_lon=-114.0, max_lat=51.1),
        TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6)),
        variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
    )
```

- `discover()` imports every module under `cfs.connectors`, triggering the
  `@register("slug")` decorators. Call it once before `get_connector()`.
- `get_connector(slug)` returns the connector **class**; instantiate it
  (optionally with a config dict, below) and use it as an async context
  manager.
- `fetch(product_id, bbox, time_range, variables=None)` returns
  `(dataset, result)`. `variables=None` means "all the product offers".
- The dataset follows the [canonical-v1](canonical-v1.md) contract and is
  **lazy** (dask-backed) where the protocol allows — slice further, then
  `.load()` / `.compute()` when ready.

To list a provider's products programmatically:

```python
async with Conn() as conn:
    for product in await conn.list_products():
        print(product.id, [v.canonical for v in product.variables])
```

## Connector configuration (config dict injection)

Connectors accept an optional `config` dict for provider-specific knobs.
Examples from the shipped connectors:

```python
# NEX-GDDP-CMIP6: choose the CMIP6 model/member (scenario is the product ID)
Conn = get_connector("nex_gddp")
conn = Conn(config={"model": "MPI-ESM1-2-HR", "member": "r1i1p1f1"})

# GEFS: fetch a subset of ensemble members (default: all 31)
conn = get_connector("gefs")(config={"members": ["gec00", "gep01", "gep02"]})

# NA-CORDEX: grid and bias-correction variant
conn = get_connector("na_cordex")(config={"grid": "NAM-22i", "bias": "mbcn-Daymet"})

# E-OBS: dataset version override
conn = get_connector("eobs")(config={"version": "30_0e"})

# EM-Earth: authenticated S3 reads
conn = get_connector("em_earth")(config={"anon": False})
```

The accepted keys are documented per connector (see each connector module's
docstring); unknown keys are ignored.

## FetchResult

`fetch()` returns the dataset **alongside** a
[`FetchResult`](reference.md#cfs.core.models.FetchResult) — a Pydantic model
capturing provenance and shape so callers can log and inspect without loading
the cube:

| Field | Meaning |
| --- | --- |
| `product_id`, `provider` | What was fetched, from which connector |
| `variables` | Canonical variables actually present in the dataset |
| `bbox`, `time_range` | The request, echoed back |
| `n_times`, `n_lat`, `n_lon` | Dataset shape (native index dims for projected grids) |
| `resolution_deg` | Native horizontal resolution |
| `lazy` | Whether the returned dataset is still dask-backed |
| `provenance` | Human-readable acquisition trail (store, cycle, processing) |
| `elapsed_ms` | Wall-clock fetch time |
| `warnings` | Advisory messages: range-QC hits, slow-path notices, unit caveats |

**Always surface `result.warnings`.** The advisory range QC reports values
outside each variable's physical range — the symptom of a unit-conversion
error — and some connectors add caveats (e.g. EM-Earth's unverified
precipitation units) that you want in your provenance records.

## Working with projected grids

Products on rotated-pole or Lambert-conformal grids (`rdrs`, `conus404`,
`hrrr`, `daymet`, `narr`, `aorc_nwm`, `nwm_operational`) keep their **native
index dimensions** (`rlat`/`rlon` or `y`/`x`) with 2-D `latitude`/`longitude`
coordinates. Consumer code must branch on this — see
[canonical-v1 § grids](canonical-v1.md#dimensions-and-coordinates) for the
exact layout and a dispatch snippet.

## Errors

All CFS exceptions live in `cfs.core.exceptions` and derive from `CFSError`:
`ConnectorError` (provider/product problems, including rate limits and
malformed upstream data), `SubsetError` (empty subset, or a guardrail refused
the request), `HarmonizationError` (no requested variable available), and
`MissingExtraError` (an optional dependency extra is not installed).

## Running it synchronously

CFS's API is async (`fetch` awaits concurrent per-file opens). For the common
case, `cfs.fetch_sync(...)` does the wrapping for you. When using the
lower-level connector pattern from synchronous code, wrap the call yourself:

```python
import asyncio

async def grab():
    async with get_connector("aorc")() as conn:
        return await conn.fetch("aorc:conus_1km", bbox, time_range)

ds, result = asyncio.run(grab())
```
