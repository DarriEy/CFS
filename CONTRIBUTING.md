# Contributing to CFS

Thanks for your interest in improving the Community Forcing Service. Bug
reports, new connectors, and documentation fixes are all welcome.

## Development setup

```bash
git clone https://github.com/DarriEy/CFS.git
cd CFS
python -m venv .venv && source .venv/bin/activate
pip install -e ".[climate,cds,earthdata,forecast,dev]"
```

The `climate` extra (xarray/zarr/s3fs/dask/netcdf4) is required for almost all
of the test suite; `cds`, `earthdata`, and `forecast` let the provider-specific
offline tests run for real instead of being skipped.

## Running the tests

```bash
pytest -m "not network"    # offline tests — no credentials or network needed
pytest -m network          # integration tests against live stores (optional)
```

All offline tests must pass before a PR is merged. Network tests hit real
upstream stores and may need credentials (CDS, Earthdata) — they are not run
in CI.

## Linting

```bash
ruff check src/ tests/
```

CI enforces `ruff` (and `mypy src/cfs`); please run both locally before
pushing.

## Adding a connector

Connectors are the most valuable contribution. The pattern:

1. **Subclass `BaseForcingConnector`** in a new module under
   `src/cfs/connectors/` (optionally mix in a protocol helper such as
   `ZarrStoreMixin`, the Earthdata OPeNDAP mixin, or the CDS API mixin from
   `cfs.connectors.protocols`).
2. **Implement `list_products()`** — the catalog metadata: product IDs
   (`{slug}:{product}`), variables, resolution, bbox, temporal extent,
   license, and citation.
3. **Implement `fetch()`** — acquire the upstream data, subset to the
   requested bbox + time range, then harmonize.
4. **Declare a `VariableMapping` table** mapping native variable names to
   canonical variables (`cfs.core.vocabulary.CanonicalVar`) with linear unit
   conversions (`canonical = source * scale + offset`), setting
   `deaccumulate=True` for running-total fields.
5. **Decorate the class with `@register("slug")`** from `cfs.core.registry`;
   `discover()` finds it automatically — no central list to edit.
6. **Finish with `self._finalize(...)`** so the shared guardrails and range QC
   apply uniformly, and return `(dataset, FetchResult)`.

Ground rules:

- The connector **stops at the canonical dataset**: no HRU remapping, no
  model-specific schemas, no serialization. That is the consumer's job.
- Precipitation and radiation must be returned as **rates** (`kg m-2 s-1`,
  `W m-2`), never accumulations.
- Return **lazy** (dask-backed) datasets where the protocol allows, so the
  caller decides when to materialize.
- Add an **offline test** (`tests/test_<slug>_offline.py`) that exercises the
  mapping/subsetting logic with synthetic data, and a `network`-marked test if
  a live fetch is feasible.
- Add the provider to `inventory/providers.yaml` and the docs
  [provider catalog](docs/catalog.md).
- If the new product's name collides with an existing acronym (looking at you,
  NOAA CFS), pick a disambiguated slug.

## Pull requests

- Keep PRs focused — one connector or one fix per PR.
- Describe what was verified: offline tests only, or a live fetch (where, what
  values, do they look physical?).
- CI must be green (ruff, mypy, offline tests on Python 3.11–3.13).

## Questions / bugs

Open an issue at <https://github.com/DarriEy/CFS/issues>. For bugs, include
the product ID, bbox, time range, and the full traceback or `FetchResult`
warnings.
