# SPDX-License-Identifier: MIT
"""Credential-free reachability sentinels for representative CFS protocols."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Sentinel:
    """A small public request representing one remote-access protocol."""

    name: str
    protocol: str
    url: str
    signatures: tuple[bytes, ...]
    headers: tuple[tuple[str, str], ...] = ()


# Keep probes metadata-sized.  These endpoints require no account and cover the
# main remote transports used by CFS without downloading forcing products.
SENTINELS = (
    Sentinel("NOAA NOMADS", "HTTP/file", "https://nomads.ncep.noaa.gov/", (b"NOMADS", b"NOAA")),
    Sentinel(
        "NOAA PSL THREDDS", "OPeNDAP DDS",
        "https://psl.noaa.gov/thredds/dodsC/Datasets/ncep.reanalysis/surface/air.sig995.2020.nc.dds",
        (b"Dataset {", b"air"),
    ),
    Sentinel(
        "AWS HRRR object", "S3 ranged object",
        "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20200101/conus/hrrr.t00z.wrfsfcf00.grib2.idx",
        (b":TMP:", b":surface:"), (("Range", "bytes=0-4095"),),
    ),
    Sentinel(
        "Google ARCO ERA5", "Zarr v3 metadata",
        "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3/zarr.json",
        (b'"zarr_format"', b'"node_type"'),
    ),
)


def probe(sentinel: Sentinel, *, timeout: float = 20.0) -> dict[str, object]:
    """Probe one sentinel, returning a stable machine-readable result."""
    started = time.monotonic()
    headers = {"User-Agent": "CFS-health-sentinel/1", **dict(sentinel.headers)}
    request = Request(sentinel.url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed public URLs
            status = int(response.status)
            payload = response.read(64 * 1024)
        missing = [token.decode("ascii", "replace") for token in sentinel.signatures if token not in payload]
        ok = 200 <= status < 400 and not missing
        error = None if ok else f"missing expected signature(s): {', '.join(missing)}"
    except HTTPError as exc:
        status, ok, error = exc.code, False, f"HTTP {exc.code}"
    except (URLError, TimeoutError) as exc:
        status, ok, error = None, False, str(exc)
    return {
        **asdict(sentinel),
        "ok": ok,
        "status": status,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "error": error,
    }


def run_sentinels(*, timeout: float = 20.0) -> dict[str, object]:
    """Run all public sentinels and return a versioned status document."""
    checks = [probe(item, timeout=timeout) for item in SENTINELS]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "healthy": all(bool(item["ok"]) for item in checks),
        "checks": checks,
    }


def main() -> int:
    report = run_sentinels()
    print(json.dumps(report, indent=2))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
