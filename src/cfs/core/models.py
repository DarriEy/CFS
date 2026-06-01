# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Canonical data models for the CFS pipeline.

Request/response and catalog metadata. The fetched payload itself is an
``xarray.Dataset`` (not modelled in Pydantic) carrying the canonical variables
defined in :mod:`cfs.core.vocabulary`; :class:`FetchResult` describes it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from cfs.core.vocabulary import CanonicalVar


class TemporalResolution(StrEnum):
    HOURLY = "hourly"
    THREE_HOURLY = "3hourly"
    SIX_HOURLY = "6hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


class Protocol(StrEnum):
    ZARR = "zarr"
    OPENDAP = "opendap"
    S3_DIRECT = "s3_direct"
    REST = "rest"


class ProviderStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    AUTH_GATED = "auth_gated"
    UNKNOWN = "unknown"


# ── Geospatial / temporal request primitives ────────────────────────


class BoundingBox(BaseModel):
    """Geographic bounding box in EPSG:4326, longitudes in [-180, 180]."""

    min_lon: float = Field(ge=-180, le=180)
    min_lat: float = Field(ge=-90, le=90)
    max_lon: float = Field(ge=-180, le=180)
    max_lat: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def _check_order(self) -> BoundingBox:
        if self.min_lat > self.max_lat:
            raise ValueError("min_lat must be <= max_lat")
        # Note: min_lon > max_lon is *allowed* — it denotes an antimeridian
        # crossing, which connectors handle explicitly during subsetting.
        return self

    @property
    def crosses_antimeridian(self) -> bool:
        return self.min_lon > self.max_lon


class TimeRange(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check_order(self) -> TimeRange:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        return self


# ── Catalog metadata ────────────────────────────────────────────────


class ProductVariable(BaseModel):
    """A canonical variable a product can deliver, plus its native source name."""

    canonical: CanonicalVar
    source_name: str = Field(description="Provider's native variable name")
    source_units: str = ""


class TemporalExtent(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    resolution: TemporalResolution = TemporalResolution.HOURLY


class ForcingProduct(BaseModel):
    """A meteorological forcing product offered by a provider."""

    id: str = Field(description="CFS-internal ID: {provider}:{product}")
    provider: str
    name: str
    description: str = ""
    variables: list[ProductVariable]
    resolution_deg: float = Field(description="Native horizontal resolution in degrees")
    crs: str = "EPSG:4326"
    bbox: BoundingBox
    temporal: TemporalExtent = Field(default_factory=TemporalExtent)
    protocol: Protocol
    license: str = ""
    citation: str = ""


# ── Fetch request / result ──────────────────────────────────────────


class FetchRequest(BaseModel):
    product_id: str
    bbox: BoundingBox
    time_range: TimeRange
    variables: list[CanonicalVar] | None = Field(
        default=None,
        description="Canonical variables to return; None = all the product offers.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "product_id": "era5_arco:single_levels",
                    "bbox": {"min_lon": -114.5, "min_lat": 50.5, "max_lon": -114.0, "max_lat": 51.2},
                    "time_range": {"start": "2010-06-01T00:00:00", "end": "2010-06-30T23:00:00"},
                    "variables": ["air_temperature", "precipitation_flux"],
                }
            ]
        }
    }


class FetchResult(BaseModel):
    """Metadata describing the ``xarray.Dataset`` a fetch produced.

    The dataset is returned alongside this object (not serialized into it). This
    captures provenance and shape so a caller can log/inspect without loading
    the cube.
    """

    product_id: str
    provider: str
    variables: list[CanonicalVar]
    bbox: BoundingBox
    time_range: TimeRange
    n_times: int
    n_lat: int
    n_lon: int
    resolution_deg: float
    crs: str = "EPSG:4326"
    lazy: bool = True
    provenance: str = ""
    elapsed_ms: int = 0
    warnings: list[str] = Field(default_factory=list)


# ── Catalog API responses ───────────────────────────────────────────


class ProviderSummary(BaseModel):
    slug: str
    name: str
    protocol: str


class HealthCheckResult(BaseModel):
    provider: str
    status: ProviderStatus
    response_time_ms: int | None = None
    last_checked: datetime
    products_available: int = 0
    error: str | None = None
