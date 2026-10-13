"""Adapters share the three tiny lifecycle utilities in ``gateway.platforms.helpers`` instead of
re-declaring them: ``cancel_task`` (cancel + await, self-cancel safe), ``MessageDeduplicator``
(TTL + size bound) and ``bounded_put`` (insertion-ordered hard cap).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

import pytest

from gateway.platforms import helpers


def test_cancel_task_unwinds_and_is_self_cancel_safe():
    async def scenario():
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(worker())
        await started.wait()
        await helpers.cancel_task(task)
        assert task.cancelled()
        await helpers.cancel_task(None)
        await helpers.cancel_task(task)  # already done: no-op

        async def suicidal():
            await helpers.cancel_task(asyncio.current_task())
            return "survived"

        me = asyncio.create_task(suicidal())
        with pytest.raises(asyncio.CancelledError):
            await me

    asyncio.run(scenario())


def test_bounded_put_refreshes_and_caps():
    store: OrderedDict = OrderedDict()
    for i in range(5):
        helpers.bounded_put(store, f"k{i}", i, cap=3)
    assert list(store) == ["k2", "k3", "k4"]
    helpers.bounded_put(store, "k2", "again", cap=3)  # refresh moves it to the newest slot
    assert list(store) == ["k3", "k4", "k2"]
