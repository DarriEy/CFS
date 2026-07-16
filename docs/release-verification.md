# Release verification matrix

CFS release candidates are checked at three levels. The offline suite is the
required merge gate; anonymous and credentialed live probes are release gates
because upstream availability and credentials are intentionally not assumed in
pull-request CI.

| Tier | Protocol families represented | Command | Required evidence |
|---|---|---|---|
| Offline | registry/catalog, harmonization, regular and projected grids, GRIB indexes, CDS/Earthdata request construction, SYMFLUENCE handoff | `pytest -m 'not network'` | all tests pass on Python 3.11–3.13 |
| Anonymous live | GCS Zarr (ERA5 ARCO), HTTP byte-range NetCDF (CHIRPS), 2-D OPeNDAP/Zarr (RDRS/CONUS404), NOAA GRIB `.idx`, ECMWF JSON index | `pytest -m network tests/test_era5_arco_network.py tests/test_chirps_network.py tests/test_grid2d_network.py tests/test_gfs_network.py tests/test_ecmwf_opendata_network.py` (omit a file if absent and record why) | canonical names/units, non-empty bbox/time subset, range QC clean |
| Credentialed live | CDS and Earthdata | `pytest -m network tests/test_auth_network.py` | each configured service passes; missing credentials are recorded as skipped |

For a release, record the date, commit, Python version, passed/skipped/failed
counts, and any upstream outage in the changelog release entry. A failure caused
by an upstream outage may be waived only when a second independent protocol
probe passes and the outage is documented.

The representative set is deliberately protocol-based rather than one test per
provider: connector-specific conversions remain covered by hermetic offline
tests, while the live matrix detects transport, authentication, index-format,
and grid-subsetting drift.
