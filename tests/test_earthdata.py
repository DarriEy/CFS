# SPDX-License-Identifier: MIT
"""Earthdata auth mixin tests — credential priority, URS session, errors."""

from __future__ import annotations

import pytest

from cfs.connectors.protocols import earthdata as ed
from cfs.connectors.protocols.earthdata import EarthdataAuthMixin, _URSSession
from cfs.core.exceptions import RegistrationRequiredError


class _Dummy(EarthdataAuthMixin):
    slug = "dummy"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    # Neutralize any real ~/.netrc so tests are deterministic.
    monkeypatch.setattr(ed.netrc, "netrc", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))


def test_token_takes_priority(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "abc123")
    monkeypatch.setenv("EARTHDATA_USERNAME", "u")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "p")
    assert _Dummy()._earthdata_credentials() == ("token", "abc123")


def test_netrc_used_when_no_token(monkeypatch):
    class _FakeNetrc:
        def authenticators(self, host):
            return ("alice", None, "secret")
    monkeypatch.setattr(ed.netrc, "netrc", lambda *a, **k: _FakeNetrc())
    assert _Dummy()._earthdata_credentials() == ("netrc", ("alice", "secret"))


def test_basic_env_used_last(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USERNAME", "bob")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "pw")
    assert _Dummy()._earthdata_credentials() == ("basic", ("bob", "pw"))


def test_missing_credentials_raises():
    assert _Dummy()._earthdata_credentials() is None
    with pytest.raises(RegistrationRequiredError):
        _Dummy()._require_earthdata()


def test_token_session_sets_bearer_header(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok")
    s = _Dummy()._earthdata_session()
    assert isinstance(s, _URSSession)
    assert s.headers["Authorization"] == "Bearer tok"


def test_urs_rebuild_auth_reapplies_token_on_urs_host():
    import requests

    s = _URSSession(token="tok")
    # Simulate a cross-host redirect from the data host to URS: requests strips
    # the Authorization header (should_strip_auth), our override re-adds it.
    prepared = requests.Request("GET", "https://urs.earthdata.nasa.gov/oauth/authorize").prepare()
    prepared.headers.pop("Authorization", None)
    resp = requests.Response()
    resp.request = requests.Request(
        "GET", "https://goldsmr4.gesdisc.eosdis.nasa.gov/opendap/x.nc4"
    ).prepare()
    resp.url = resp.request.url

    s.rebuild_auth(prepared, resp)
    assert prepared.headers["Authorization"] == "Bearer tok"
