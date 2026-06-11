# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Integrations of CFS with downstream modeling frameworks.

Each submodule adapts the CFS facade (:func:`cfs.fetch_sync`) to one
framework's plugin contract. Integration modules import their framework
*defensively*: importing :mod:`cfs` (or the integration module itself) never
fails when the framework is not installed.
"""
