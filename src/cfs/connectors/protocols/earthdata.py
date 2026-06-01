# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""NASA Earthdata Login (URS) authenticated OPeNDAP access mixin.

GES DISC and other NASA DAACs guard their OPeNDAP endpoints with Earthdata Login.
The flow is redirect-based: a data request is bounced to ``urs.earthdata.nasa.gov``
to authenticate, then back to the data host with a cookie. The ``requests``
library strips the ``Authorization`` header on cross-host redirects, so a plain
session loses its credentials at the URS hop — hence :class:`_URSSession`, which
re-applies auth specifically on the URS host (the standard NASA recipe).

CFS opens these endpoints *lazily* with the ``pydap`` xarray engine, so a regular
``.sel(lat=..., lon=..., time=...)`` subset pulls only the requested hyperslabs
over the wire — true acquire-and-subset, and it reuses :mod:`cfs.subset.bbox`
because these grids are regular lat/lon.

Credentials are detected in priority order:
  1. ``EARTHDATA_TOKEN``  (bearer token — most robust)
  2. ``~/.netrc``         (machine urs.earthdata.nasa.gov)
  3. ``EARTHDATA_USERNAME`` / ``EARTHDATA_PASSWORD``
Missing → a clear RegistrationRequiredError pointing at URS registration.

``pydap`` lives in the optional ``earthdata`` extra and is imported lazily so the
module stays importable for registry discovery without it.
"""

from __future__ import annotations

import netrc
import os
from urllib.parse import urlparse

import requests
import structlog

from cfs.core.exceptions import MissingExtraError, RegistrationRequiredError

logger = structlog.get_logger(__name__)

URS_HOST = "urs.earthdata.nasa.gov"
_URS_REGISTER = "https://urs.earthdata.nasa.gov/users/new"


class _URSSession(requests.Session):
    """requests Session that re-applies auth across the URS redirect."""

    def __init__(self, token: str | None = None, userpass: tuple[str, str] | None = None) -> None:
        super().__init__()
        self._token = token
        self._userpass = userpass
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        elif userpass:
            self.auth = userpass

    def rebuild_auth(self, prepared_request, response) -> None:
        super().rebuild_auth(prepared_request, response)
        if urlparse(prepared_request.url).hostname == URS_HOST:
            if self._token:
                prepared_request.headers["Authorization"] = f"Bearer {self._token}"
            elif self._userpass:
                prepared_request.prepare_auth(self._userpass)


class EarthdataAuthMixin:
    """Mixin providing Earthdata-authenticated OPeNDAP access."""

    slug: str

    def _earthdata_credentials(self) -> tuple[str, object] | None:
        """Return ('token', str) | ('netrc'|'basic', (user, pass)) | None."""
        token = os.environ.get("EARTHDATA_TOKEN")
        if token:
            return ("token", token)
        try:
            auth = netrc.netrc().authenticators(URS_HOST)
            if auth and auth[0]:
                return ("netrc", (auth[0], auth[2]))
        except (FileNotFoundError, netrc.NetrcParseError):
            pass
        user = os.environ.get("EARTHDATA_USERNAME")
        pw = os.environ.get("EARTHDATA_PASSWORD")
        if user and pw:
            return ("basic", (user, pw))
        return None

    def _require_earthdata(self) -> tuple[str, object]:
        cred = self._earthdata_credentials()
        if cred is None:
            raise RegistrationRequiredError(
                self.slug,
                _URS_REGISTER,
                "No NASA Earthdata credentials found. Provide one of:\n"
                "  1. export EARTHDATA_TOKEN=<token>  (URS → My Profile → Generate Token)\n"
                "  2. ~/.netrc with: machine urs.earthdata.nasa.gov login <u> password <p>\n"
                "  3. EARTHDATA_USERNAME and EARTHDATA_PASSWORD env vars\n"
                "Also authorize 'NASA GESDISC DATA ARCHIVE' under URS → Applications.",
            )
        return cred

    def _earthdata_session(self) -> _URSSession:
        kind, val = self._require_earthdata()
        if kind == "token":
            return _URSSession(token=val)  # type: ignore[arg-type]
        return _URSSession(userpass=val)  # type: ignore[arg-type]

    def _open_opendap(self, url: str):
        """Open an authenticated OPeNDAP endpoint as a lazy xarray Dataset."""
        import xarray as xr

        try:
            import pydap  # noqa: F401
        except ImportError as e:  # pragma: no cover - only without the extra
            raise MissingExtraError(
                "Earthdata OPeNDAP access needs the 'earthdata' extra: "
                "pip install -e '.[earthdata]' (pydap)"
            ) from e
        return xr.open_dataset(url, engine="pydap", session=self._earthdata_session())
