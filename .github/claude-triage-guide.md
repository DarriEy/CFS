# CI Triage Guide — CFS (Community Forcing Service)

This file is read by the automated CI triage agent (`.github/workflows/ci-autotriage.yml`).
It defines how to classify a CI failure for **this** service and what is safe to auto-fix.

## What this service is

CFS connectors acquire a meteorological forcing product and return a **harmonized,
bbox-and-time-subset xarray cube in canonical variables and units**. A connector's job
ends there — it must NOT remap to HRUs, write model-specific schemas, or serialize to disk.

## Classifications and actions

Pick exactly one. The action column is enforced by the workflows — the auto-merge job merges **only** `adapter_drift`/`data_drift` fixes, **only** when every
changed file is under `src/<svc>/connectors/` or `tests/`, **only** when the fix does not change
an expected value/assertion (the *truth gate*), and **only** when the connector has not tripped
the *circuit breaker* (repeated recent autofixes).

| Classification | What it means | Action |
|---|---|---|
| `adapter_drift` | A forcing **data provider** changed something a connector consumes (native variable name, unit, dimension order, endpoint path, response field). Fixable **entirely inside `src/cfs/connectors/<slug>.py`** and/or its offline test. | Fix PR → **auto-merge on green** |
| `data_drift` | Contract and live provider are fine, but a recorded fixture / expected value in an offline test is stale. Fixable inside `connectors/` or `tests/`. | Fix PR → **human merge** — truth gate (expected-value change) |
| `contract_change` | The failure involves the canonical schema/contract — anything under `src/cfs/core/` (`models.py`, `vocabulary.py`, `exceptions.py`, `registry.py`, `config.py`) or a public interface. | Fix PR → **human merge** |
| `tooling_drift` | A build / CI / dependency / tooling failure: mypy or ruff config, a dependency version bump (numpy, pandas, xarray, …), type stubs, packaging, or the CI workflow itself. **Not** a data-provider change. | Fix PR → **human merge** |
| `outage` | Transient external failure: HTTP 429/5xx, DNS, connection timeouts, CDS/Earthdata auth-service hiccups. | **Report only** (recommend re-run) |
| `real_bug` | A genuine logic error in non-adapter CFS code. | **Report only** (describe the fix) |
| `other` | You cannot confidently classify it. | **Report only** |

## The canonical contract — never auto-fixed

Editing anything under `src/cfs/core/` is a `contract_change` (human-only), never drift:
- `models.py` — `ForcingProduct`, `ProductVariable`, `FetchResult`, the `fetch()` return shape.
- `vocabulary.py` — canonical variable names + units and the `VariableMapping` table (canonical
  vars are **rates**: `AIR_TEMPERATURE` K, `PRECIPITATION_FLUX` kg m-2 s-1,
  `SHORTWAVE_RADIATION_DOWN` W m-2, `SPECIFIC_HUMIDITY` kg kg-1 — never accumulations).
- `exceptions.py`, `registry.py`, `config.py` — public interfaces.

## The scope rule (critical — read before opening any fix PR)

An `adapter_drift` / `data_drift` fix **must change only files under `src/cfs/connectors/` or
`tests/`**. If the minimal fix would touch **any** other path — `pyproject.toml`, `.github/`,
`src/cfs/core/`, docs, packaging — then it is **not** adapter/data drift. Reclassify:
- touches `src/cfs/core/` → `contract_change`
- touches build/CI/deps (e.g. `pyproject.toml` mypy/ruff/version config) → `tooling_drift`

Both take the **human-gated** path (label `needs-human-review`, never `automerge-on-green`).
"Upstream changed" applies to **data providers**, not to libraries like numpy/mypy — a stub or
dependency-version break is `tooling_drift`, not `adapter_drift`. The auto-merge job will refuse
to merge any PR that changes files outside `connectors/`/`tests/`, even if mislabeled.

## The truth gate (enforced by the auto-merge job — read before touching `tests/`)

You may auto-fix **how a connector fetches or parses** (endpoints, variable/band/coverage ids,
dimension order, response fields) — that is `adapter_drift`, and it auto-merges on green. You may
**not** silently auto-canonize **what the truth is**. The auto-merge job scans the diff and routes
to a human:

- **Any change to a non-`.py` file under `tests/`** — a recorded fixture / response / blob
  (`.json`, `.csv`, `.nc`, parquet, pickle, …). "Refresh the recording to what the provider now
  serves" is exactly the garbage-canonization case and it carries no keyword, so the whole file
  class is gated on sight.
- **In `tests/*.py`**: a changed assertion, a semantic token (a `units` string, a CRS/`EPSG` code,
  a quality-flag enum `GOOD`/`SUSPECT`/`PARTIAL`/`MISSING`/`DEGRADED`/`ESTIMATED`), an
  `expected`/`EXPECTED` constant, **or any changed line carrying a bare numeric literal** — this
  catches `EXPECTED_DEM_MEAN = 132.4 → 187.2`, whose value moves without an `assert` or `==` on
  the line.

Why: updating a stale recorded expectation and rubber-stamping a provider that has silently started
serving wrong data (a units flip, a compromised layer, a swapped coverage) are the **same edit** —
you cannot tell them apart from inside CI. So a `data_drift` fix that changes an expected value is
**human-gated**, not auto-merged. Label it `needs-human-review`, make the minimal change, and
explain in the PR why the new value is the correct truth. Keyword-free, number-free test changes
that only touch **mocks / setup / imports** still auto-merge.

## The circuit breaker (a human-controlled latch, enforced by the auto-merge job)

The **tracking issue is the breaker state**. While a `🚨 Repeated drift autofixes: <connector>`
issue is **open**, auto-merge is paused for that connector — full stop. **Closing the issue
re-arms** auto-merge, and only autofix merges *after* the close count toward the next trip (so the
merges that tripped it do not immediately re-trip). The first trip fires when a connector has been
auto-fixed **3+ times in 7 days** (and since the last re-arm). Repeated mechanical drift on one
provider is itself the signal that the **provider relationship** needs a person — a staged endpoint
deprecation, a churning coverage id, an auth/format change in flight — not another squash-merge.
Nothing you can do in a fix PR bypasses this; the human's close is the only re-arm.

## CI commands (what "green" means here)

```
ruff check src/ tests/
mypy src/cfs
pytest tests/ -v --tb=short -m "not network"
```

Never make CI pass by skipping/weakening tests, loosening assertions, or marking things `network`
to deselect them. Fix the cause or classify honestly.
