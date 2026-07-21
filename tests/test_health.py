"""Tests for credential-free remote health sentinels."""

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from cfs.health import SENTINELS, Sentinel, probe, run_sentinels


def test_sentinels_cover_distinct_protocol_families():
    assert {item.protocol for item in SENTINELS} == {
        "HTTP/file", "OPeNDAP DDS", "S3 ranged object", "Zarr v3 metadata",
    }
    assert len({item.url for item in SENTINELS}) == len(SENTINELS)


def test_probe_success():
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    response.read.return_value = b"expected metadata"
    with patch("cfs.health.urlopen", return_value=response):
        result = probe(Sentinel("example", "HTTP", "https://example.test", (b"metadata",)))
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["error"] is None


def test_probe_reports_http_and_network_failures():
    item = Sentinel("example", "HTTP", "https://example.test", (b"metadata",))
    with patch("cfs.health.urlopen", side_effect=HTTPError(item.url, 503, "down", {}, None)):
        assert probe(item)["status"] == 503
    with patch("cfs.health.urlopen", side_effect=URLError("offline")):
        result = probe(item)
    assert result["ok"] is False
    assert "offline" in str(result["error"])


def test_probe_rejects_an_unexpected_success_page_and_sends_range_header():
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    response.read.return_value = b"generic service landing page"
    item = Sentinel(
        "example", "S3 ranged object", "https://example.test/object",
        (b":TMP:",), (("Range", "bytes=0-4095"),),
    )
    with patch("cfs.health.urlopen", return_value=response) as opened:
        result = probe(item)
    assert result["ok"] is False
    assert "missing expected signature" in str(result["error"])
    assert opened.call_args.args[0].get_header("Range") == "bytes=0-4095"


def test_report_has_stable_machine_readable_shape():
    with patch("cfs.health.probe", side_effect=[
        {"ok": True}, {"ok": True}, {"ok": True}, {"ok": False},
    ]):
        report = run_sentinels()
    assert report["schema_version"] == 1
    assert report["healthy"] is False
    assert len(report["checks"]) == 4
    assert str(report["generated_at"]).endswith("+00:00")
