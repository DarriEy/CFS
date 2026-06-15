"""CFS — Community Forcing Service.

Acquire-and-subset access to meteorological forcing products for hydrological
modelling. Every connector pulls from an upstream product (cloud Zarr, OPeNDAP,
S3, …), subsets to a bounding box and time range, harmonizes variable names and
units to a **canonical, model-agnostic schema**, and returns an ``xarray.Dataset``.

CFS deliberately stops at the canonical gridded dataset. Remapping to HRUs and
writing model-specific forcing schemas (SUMMA, FUSE, …) is the consumer's job —
e.g. SYMFLUENCE — keeping this service reusable across frameworks.

The blessed public surface is re-exported here::

    import cfs

    ds, result = cfs.fetch_sync(
        "era5_arco:single_levels",
        bbox=(-114.5, 50.7, -114.0, 51.1),
        time_range=("2015-06-01T00:00", "2015-06-01T06:00"),
        variables=["air_temperature", "precipitation_flux"],
    )

Connector modules stay lazy — they are only imported when :func:`discover`
runs (directly, or via :func:`fetch` / :func:`fetch_sync`).
"""

from cfs.api import configure, fetch, fetch_sync
from cfs.core.models import BoundingBox, FetchResult, TimeRange
from cfs.core.registry import discover, get_connector, list_providers
from cfs.core.vocabulary import CanonicalVar

__version__ = "0.5.0"

__all__ = [
    "BoundingBox",
    "CanonicalVar",
    "FetchResult",
    "TimeRange",
    "__version__",
    "configure",
    "discover",
    "fetch",
    "fetch_sync",
    "get_connector",
    "list_providers",
]
