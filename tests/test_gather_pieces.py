# SPDX-License-Identifier: MIT
"""Offline tests for BaseForcingConnector._gather_pieces (concurrency helper).

These exercise the per-file fan-out used by the NLDAS/HRRR/MERRA-2/Daymet/GPM/
CHIRPS connectors without touching the network: thunks are plain callables.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from cfs.connectors.base import BaseForcingConnector


class _Dummy(BaseForcingConnector):
    slug = "dummy"
    display_name = "dummy"
    base_url = ""
    protocol = "test"

    async def list_products(self):  # pragma: no cover - unused
        return []

    async def fetch(self, *a, **k):  # pragma: no cover - unused
        raise NotImplementedError


def test_gather_preserves_order_and_drops_none():
    conn = _Dummy()
    # Out-of-order completion (early items sleep longer) must still come back
    # in input order; None thunks are dropped.
    thunks = [
        lambda: (time.sleep(0.03), 0)[1],
        lambda: None,
        lambda: (time.sleep(0.01), 2)[1],
        lambda: 3,
    ]
    out = asyncio.run(conn._gather_pieces(thunks, concurrency=4))
    assert out == [0, 2, 3]


def test_gather_respects_concurrency_bound():
    conn = _Dummy()
    lock = threading.Lock()
    state = {"active": 0, "max": 0}

    def make(i):
        def thunk():
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return i
        return thunk

    thunks = [make(i) for i in range(10)]
    out = asyncio.run(conn._gather_pieces(thunks, concurrency=3))
    assert out == list(range(10))
    assert state["max"] <= 3  # never more than `concurrency` thunks at once


def test_gather_runs_concurrently_faster_than_serial():
    conn = _Dummy()
    thunks = [lambda: (time.sleep(0.05), 1)[1] for _ in range(8)]
    t0 = time.monotonic()
    out = asyncio.run(conn._gather_pieces(thunks, concurrency=8))
    elapsed = time.monotonic() - t0
    assert len(out) == 8
    # 8 × 50 ms serial = 400 ms; concurrent should be well under half that.
    assert elapsed < 0.2


def test_gather_propagates_exceptions():
    conn = _Dummy()

    def boom():
        raise ValueError("kaboom")

    thunks = [lambda: 1, boom, lambda: 3]
    with pytest.raises(ValueError, match="kaboom"):
        asyncio.run(conn._gather_pieces(thunks, concurrency=4))


def test_gather_serial_when_concurrency_one():
    conn = _Dummy()
    order = []
    thunks = [lambda i=i: (order.append(i), i)[1] for i in range(5)]
    out = asyncio.run(conn._gather_pieces(thunks, concurrency=1))
    assert out == [0, 1, 2, 3, 4]
    assert order == [0, 1, 2, 3, 4]
