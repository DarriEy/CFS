# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Rclone (Google Drive) access mixin.

A few hydrology datasets are distributed only as files on a Google Drive folder
shared with registered users — MSWEP is the notable one. There is no public API
or S3 mirror; access is via the ``rclone`` CLI configured with a Drive remote
that has been granted access. This mixin shells out to ``rclone cat`` to stream a
single remote file's bytes (no temp files), which the connector then opens from
memory.

This is the one genuinely out-of-band access path in CFS: it needs the external
``rclone`` binary *and* the user to have requested access to the dataset's Drive
folder. Absence of either surfaces as a clear RegistrationRequiredError.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - invoking the user's configured rclone CLI

import structlog

from cfs.core.exceptions import ConnectorError, RegistrationRequiredError

logger = structlog.get_logger(__name__)


class RcloneMixin:
    """Mixin providing single-file reads from an rclone Google Drive remote."""

    slug: str
    rclone_register_url: str = "https://www.gloh2o.org/mswep/"

    def _rclone_bin(self) -> str:
        binary = shutil.which("rclone")
        if not binary:
            raise RegistrationRequiredError(
                self.slug,
                self.rclone_register_url,
                "rclone not found on PATH. To use this provider:\n"
                "  1. Request data access (e.g. register at the URL above);\n"
                "  2. Install rclone: https://rclone.org/downloads/\n"
                "  3. `rclone config` → create a Google Drive remote;\n"
                "  4. Verify: rclone lsd --drive-shared-with-me <remote>:",
            )
        return binary

    def _rclone_remotes(self) -> list[str]:
        """Names of the remotes configured in the user's rclone config."""
        proc = subprocess.run(  # nosec B603
            [self._rclone_bin(), "listremotes"], capture_output=True, text=True,
            timeout=30, check=False,
        )
        return [ln.rstrip(":").strip() for ln in proc.stdout.splitlines() if ln.strip().endswith(":")]

    def _require_rclone_remote(self, remote: str) -> None:
        """Fail fast with a clear error if ``remote`` isn't configured in rclone."""
        configured = self._rclone_remotes()
        if remote not in configured:
            raise RegistrationRequiredError(
                self.slug,
                self.rclone_register_url,
                f"rclone remote '{remote}' is not configured "
                f"(found: {configured or 'none'}). Run `rclone config` to create a "
                "Google Drive remote with access to the shared dataset folder, or set "
                "the remote name via config/env.",
            )

    def _rclone_cat(self, remote: str, path: str, *, timeout: float = 600.0) -> bytes:
        """Stream the bytes of ``{remote}:{path}`` from a shared Drive folder."""
        cmd = [
            self._rclone_bin(),
            "cat",
            "--drive-shared-with-me",
            f"{remote}:{path}",
        ]
        logger.debug("rclone cat", remote=remote, path=path)
        try:
            proc = subprocess.run(  # nosec B603 - args are list form, no shell
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as e:
            raise ConnectorError(self.slug, f"rclone cat timed out for {path}") from e
        if proc.returncode != 0:
            raise ConnectorError(
                self.slug, f"rclone cat failed for {path}: {proc.stderr.decode()[:200]}"
            )
        if not proc.stdout:
            raise ConnectorError(self.slug, f"rclone returned no data for {path}")
        return proc.stdout
