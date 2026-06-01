"""CFS — Community Forcing Service.

Acquire-and-subset access to meteorological forcing products for hydrological
modelling. Every connector pulls from an upstream product (cloud Zarr, OPeNDAP,
S3, …), subsets to a bounding box and time range, harmonizes variable names and
units to a **canonical, model-agnostic schema**, and returns an ``xarray.Dataset``.

CFS deliberately stops at the canonical gridded dataset. Remapping to HRUs and
writing model-specific forcing schemas (SUMMA, FUSE, …) is the consumer's job —
e.g. SYMFLUENCE — keeping this service reusable across frameworks.
"""

__version__ = "0.1.0"
