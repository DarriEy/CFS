---
name: Bug report
about: A fetch failed, returned wrong values, or something else broke
labels: bug
---

## What happened

A clear description of the problem.

## How to reproduce

```python
# Minimal example — product ID, bbox, time range, variables
```

- **Product ID** (e.g. `era5_arco:single_levels`):
- **Bounding box** (`min_lon,min_lat,max_lon,max_lat`):
- **Time range**:
- **Credentials involved?** (CDS / Earthdata / AWS — never paste them):

## What you expected

## Output

Full traceback, or the `FetchResult` (especially `warnings`) if the fetch
succeeded but the values look wrong.

```
paste here
```

## Environment

- CFS version (`cfs --version`):
- Python version / OS:
- Install extras used (`climate`, `cds`, `earthdata`, `forecast`):
