## What

One or two sentences: what does this PR change and why?

## Type

- [ ] New connector
- [ ] Bug fix
- [ ] Docs / infrastructure
- [ ] Other

## Verification

- [ ] `ruff check src/ tests/` passes
- [ ] `mypy src/cfs` passes
- [ ] `pytest -m "not network"` passes
- [ ] New code has offline tests (synthetic data; no network)

For connectors: describe what you verified against the live store —
bbox/period fetched, and whether the returned values look physical
(temperatures in K, precip flux ~1e-4 not ~10, radiation W m-2).

## Checklist (connectors only)

- [ ] Stops at the canonical dataset (no HRU remapping, no model schemas)
- [ ] Precip/radiation returned as rates, never accumulations
- [ ] `VariableMapping` table with unit conversions; ends with `self._finalize(...)`
- [ ] Added to `inventory/providers.yaml` and `docs/catalog.md`
