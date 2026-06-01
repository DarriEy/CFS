# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026 CFS Contributors
"""Derived-variable physics for forcing harmonization.

Some providers don't ship a canonical variable directly — CARRA/CERRA give
2 m *relative* humidity, not specific humidity. These helpers compute the
canonical field from the available inputs, using standard, well-cited formulae,
so the derivation lives in one tested place rather than scattered in connectors.
They operate elementwise and stay lazy on dask-backed xarray inputs.
"""
