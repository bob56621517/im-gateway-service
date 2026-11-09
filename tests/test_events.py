"""Extra unit tests for the in-process :class:`EventBus` (gateway/events.py).

Covers the branches the gateway integration tests intentionally avoid:
no-subscriber publishes, explicit loop binding, cross-thread publishing, the
"loop is gone" fallback, bounded-queue drop-oldest, and subscriber lifecycle.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from im_gateway.gateway import events
from im_gateway.gateway.events import EventBus

# ---------------------------------------------------------------------------
# publish without consumers / loop binding
# ---------------------------------------------------------------------------


def test_publish_without_subscribers_is_a_noop() -> None:
    bus = EventBus()
    bus.publish({"type": "channel.state"})  # must not raise / must not queue anything
    assert bus.subscriber_count == 0


async def test_bind_loop_with_explicit_loop() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bus.bind_loop(loop)
    assert bus._loop is loop


async def test_bind_loop_defaults_to_running_loop() -> None:
    bus = EventBus()
    bus.bind_loop()
    assert bus._loop is asyncio.get_running_loop()


def test_publish_falls_back_to_direct_delivery_when_loop_is_closed() -> None:
    bus = EventBus()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    bus._subscribers.add(queue)  # type: ignore[attr-defined]

    class _ClosedLoop:
        def is_running(self) -> bool:
            return True

        def call_soon_threadsafe(self, *args: Any) -> None:
            raise RuntimeError("loop is closed")

    bus._loop = _ClosedLoop()  # type: ignore[assignment]
    bus.publish({"kind": "direct"})
    assert queue.get_nowait() == {"kind": "direct"}


def test_publish_without_bound_loop_delivers_directly() -> None:
    bus = EventBus()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    bus._subscribers.add(queue)  # type: ignore[attr-defined]

    # No running loop in this synchronous test -> ``get_running_loop`` raises.
    bus.publish({"kind": "sync"})
    assert queue.get_nowait() == {"kind": "sync"}


# ---------------------------------------------------------------------------
# Bounded queue: oldest event is dropped
# ---------------------------------------------------------------------------


def test_full_queue_drops_oldest_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "QUEUE_MAXSIZE", 2)
    bus = EventBus()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)
    bus._subscribers.add(queue)  # type: ignore[attr-defined]

    for index in range(3):
        bus.publish({"seq": index})

    remaining = [queue.get_nowait()["seq"] for _ in range(2)]
    assert remaining == [1, 2]


def test_subscriber_count_tracks_manual_subscribers() -> None:
    bus = EventBus()
    assert bus.subscriber_count == 0
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    bus._subscribers.add(queue)  # type: ignore[attr-defined]
    assert bus.subscriber_count == 1
    bus._subscribers.discard(queue)  # type: ignore[attr-defined]
    assert bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# subscribe lifecycle / cross-thread publish
# ---------------------------------------------------------------------------


async def test_subscribe_binds_loop_and_removes_on_close() -> None:
    bus = EventBus()
    assert bus._loop is None

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        iterator = bus.subscribe()
        try:
            async for event in iterator:
                received.append(event)
                return
        finally:
            await iterator.aclose()

    task = asyncio.create_task(consume())
    for _ in range(100):
        if bus.subscriber_count:
            break
        await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    assert bus._loop is asyncio.get_running_loop()

    bus.publish({"from": "loop"})
    await asyncio.wait_for(task, timeout=1.0)
    assert received == [{"from": "loop"}]
    assert bus.subscriber_count == 0


async def test_publish_from_non_loop_thread_reaches_subscriber() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    received: list[dict[str, Any]] = []

    async def consume() -> None:
        iterator = bus.subscribe()
        try:
            async for event in iterator:
                received.append(event)
                return
        finally:
            await iterator.aclose()

    task = asyncio.create_task(consume())
    for _ in range(100):
        if bus.subscriber_count:
            break
        await asyncio.sleep(0)
    assert bus.subscriber_count == 1

    def producer() -> None:
        bus.publish({"from": "thread"})

    thread = threading.Thread(target=producer)
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()

    await asyncio.wait_for(task, timeout=1.0)
    assert received == [{"from": "thread"}]
    assert bus.subscriber_count == 0
