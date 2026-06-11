---
name: Connector request
about: Propose a new forcing product for CFS
labels: connector
---

## Product

- **Name** (e.g. "NOAA CFSR"):
- **Provider / archive** (who hosts it):
- **Access** (cloud Zarr, OPeNDAP, S3 NetCDF/GRIB2, HTTP, CDS API, ...):
- **URL of the store or landing page**:

## Coverage

- **Spatial domain + resolution**:
- **Temporal extent + resolution**:
- **Grid** (regular lat/lon, or projected/rotated — which projection?):

## Variables

Which canonical variables can it supply (air_temperature,
precipitation_flux, winds, humidity, pressure, SW/LW radiation)?
Note anything tricky: accumulated precip/radiation, RH instead of
specific humidity, scalar wind speed only, unusual units.

## Auth and license

- **Authentication** (anonymous, Earthdata, CDS, API key, ...):
- **Data license**:

## Why

One or two sentences on the modeling use case this unlocks
(region, period, resolution that existing connectors don't cover).

Willing to implement it yourself? See
[CONTRIBUTING.md](https://github.com/DarriEy/CFS/blob/main/CONTRIBUTING.md)
for the connector pattern — PRs welcome.
