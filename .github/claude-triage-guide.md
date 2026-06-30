# CI Triage Guide — CFS (Community Forcing Service)

This file is read by the automated CI triage agent (`.github/workflows/ci-autotriage.yml`).
It defines how to classify a CI failure for **this** service and what is safe to auto-fix.

## What this service is

CFS connectors acquire a meteorological forcing product and return a **harmonized,
bbox-and-time-subset xarray cube in canonical variables and units**. A connector's job
ends there — it must NOT remap to HRUs, write model-specific schemas, or serialize to disk.

## The canonical contract — DO NOT auto-modify

The contract is the boundary every connector must honor. Changes here are **`contract_change`**
(human-only), never `adapter_drift`:

- `src/cfs/core/models.py` — `ForcingProduct`, `ProductVariable`, `FetchResult`, the `fetch()` return shape.
- `src/cfs/core/vocabulary.py` — canonical variable names + units and the `VariableMapping` table
  (canonical vars are **rates**: `AIR_TEMPERATURE` K, `PRECIPITATION_FLUX` kg m-2 s-1,
  `SHORTWAVE_RADIATION_DOWN` W m-2, `SPECIFIC_HUMIDITY` kg kg-1 — never accumulations).
- `src/cfs/core/exceptions.py`, `registry.py`, `config.py` — public interfaces.

If a fix would require editing anything under `src/cfs/core/`, it is a **`contract_change`**. Stop and
open a human-review PR instead of auto-fixing.

## What is safe to auto-fix (adapter / data drift)

Confined to a single provider under `src/cfs/connectors/<slug>.py` and/or its offline test
`tests/test_<slug>_offline.py`, WITHOUT touching the contract:

- **adapter_drift** — upstream changed a native variable name, unit, dimension order, endpoint
  path, or response field, and the connector's mapping/parsing must follow. Tell-tale:
  `HarmonizationError`, a `VariableMapping` lookup miss, a unit conversion assertion, a KeyError
  on a renamed source field.
- **data_drift** — the contract and live provider are fine, but a recorded fixture / expected
  value in an offline test is stale. Fix = update the connector or the synthetic fixture.

## Classify as report-only (never auto-fix)

- **outage** — transient external failure: HTTP 429/5xx, DNS, connection timeouts, CDS/Earthdata
  auth-service hiccups. Recommend a re-run; do not change code.
- **contract_change** — see above; touches `src/cfs/core/`.
- **real_bug** — a genuine logic error in non-adapter CFS code. Describe the fix, leave it to a human.

## CI commands (what "green" means here)

```
ruff check src/ tests/
mypy src/cfs
pytest tests/ -v --tb=short -m "not network"
```

Never make CI pass by skipping/weakening tests, loosening assertions, or marking things `network`
to deselect them. Fix the cause or classify honestly.
