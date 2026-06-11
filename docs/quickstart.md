# Quick Start

## Install

```bash
pip install 'community-forcing-service[climate]'   # xarray, zarr, gcsfs, dask, netcdf4
```

The PyPI distribution is named `community-forcing-service` (the name `cfs` is
taken on PyPI); the package you import is `cfs` and the CLI command is `cfs`.

The `climate` extra pulls in the array stack (xarray, zarr, fsspec, gcsfs,
s3fs, dask, netcdf4) that almost every connector needs. Additional extras gate
provider-specific dependencies:

| Extra | Pulls in | Needed for |
| --- | --- | --- |
| `climate` | xarray, zarr, gcsfs, s3fs, dask, netcdf4, … | nearly all connectors |
| `cds` | cdsapi | `era5_land`, `era5_cds`, `wfde5`, `carra`, `cerra`, `eobs` |
| `earthdata` | pydap, pandas, pyproj | `merra2`, `nldas`, `gpm`, `gldas`, `fldas`, `daymet` |
| `forecast` | cfgrib, eccodes, pandas | `gfs`, `gefs` |

To work from a source checkout instead:

```bash
git clone https://github.com/DarriEy/CFS.git && cd CFS
pip install -e ".[climate,cds,earthdata,forecast,dev]"
```

## Credentials

Anonymous connectors (ERA5 ARCO, AORC, CHIRPS, HRRR, CONUS404, GFS/GEFS, …)
need nothing. Otherwise:

- **CDS connectors** need `~/.cdsapirc`
  ([CDS API how-to](https://cds.climate.copernicus.eu/how-to-api)), plus the
  relevant dataset licences accepted on your CDS account.
- **Earthdata connectors** need `EARTHDATA_TOKEN` (or `~/.netrc` /
  `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`) with the "NASA GESDISC DATA
  ARCHIVE" application authorized under your URS profile.

See the [Provider Catalog](catalog.md) for per-provider auth requirements.

## 1. Explore the catalog

```bash
cfs providers                    # list registered provider slugs
cfs products                     # list products + canonical variables
cfs products -p era5_arco        # one provider only
```

## 2. Fetch from the CLI

```bash
cfs fetch \
  -P era5_arco:single_levels \
  -b -114.5,50.7,-114.0,51.1 \
  --start 2015-06-01T00:00 --end 2015-06-01T06:00 \
  -v air_temperature,precipitation_flux
```

This prints the `FetchResult` metadata as JSON. Add `-o forcing.nc` to also
write the canonical cube to NetCDF (a convenience for inspection —
model-schema writing is the consumer's job, not CFS's).

## 3. Fetch from Python

```python
from datetime import datetime

from cfs.core.models import BoundingBox, TimeRange
from cfs.core.registry import discover, get_connector
from cfs.core.vocabulary import CanonicalVar

discover()
Conn = get_connector("era5_arco")
async with Conn() as conn:
    ds, result = await conn.fetch(
        "era5_arco:single_levels",
        BoundingBox(min_lon=-114.5, min_lat=50.7, max_lon=-114.0, max_lat=51.1),
        TimeRange(start=datetime(2015, 6, 1, 0), end=datetime(2015, 6, 1, 6)),
        variables=[CanonicalVar.AIR_TEMPERATURE, CanonicalVar.PRECIPITATION_FLUX],
    )
# ds: lazy canonical cube;  result: FetchResult provenance/shape metadata
```

The returned dataset follows the [canonical-v1](canonical-v1.md) contract:
CF-aligned names, SI units, precipitation and radiation as rates. See the
[Python API guide](python-api.md) for connector configuration, the
`FetchResult` fields, and the projected-grid layout consumers must handle.

!!! note "Live upstreams"
    CFS is a passthrough service — every fetch hits the provider's live store,
    so transient upstream outages (a THREDDS restart, an S3 hiccup, CDS queue
    congestion) can surface as fetch errors. Retry later before suspecting CFS.
