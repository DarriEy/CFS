# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""CFS exception hierarchy (parallels CAS)."""

from __future__ import annotations


class CFSError(Exception):
    """Base exception for all CFS errors."""


class ConnectorError(CFSError):
    """Raised when a forcing provider connector fails."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class RateLimitError(ConnectorError):
    """Provider rate-limited us — triggers automatic retry."""


class DataFormatError(ConnectorError):
    """Provider response/store doesn't match the expected format."""


class ProtocolError(ConnectorError):
    """Protocol-level operation failed (Zarr, OPeNDAP, S3)."""


class GeometryError(CFSError):
    """Input bounding box is invalid or unsupported."""


class SubsetError(CFSError):
    """Spatial/temporal subsetting produced an empty or invalid result."""


class HarmonizationError(CFSError):
    """A source variable could not be mapped to the canonical schema."""


class MissingExtraError(CFSError):
    """An optional dependency extra (e.g. 'climate') is not installed."""


class RegistrationRequiredError(ConnectorError):
    """Provider requires registration/API key that is not configured."""

    def __init__(self, provider: str, registration_url: str, instructions: str) -> None:
        self.registration_url = registration_url
        self.instructions = instructions
        super().__init__(
            provider,
            f"Registration required. Register at: {registration_url}\n{instructions}",
        )
