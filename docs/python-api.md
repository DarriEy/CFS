# Python API

CFS is used in-process: discover the connector registry, instantiate a
connector, and `await` a fetch inside an async context. There is no server.

## The discover / get_connector / fetch pattern

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

CFS's API is async (`fetch` awaits concurrent per-file opens). From
synchronous code, wrap the call:

```python
import asyncio

async def grab():
    async with get_connector("aorc")() as conn:
        return await conn.fetch("aorc:conus_1km", bbox, time_range)

ds, result = asyncio.run(grab())
```
