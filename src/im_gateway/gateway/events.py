"""In-process event bus for the control plane (``GET /v1/events`` SSE).

The daemon is a single process with a single asyncio event loop, so the bus is
deliberately minimal: no persistence, no replay, no cross-process delivery.

Threading contract (HANDOVER §11.2 #17 — channel SDK callbacks arrive on their
own threads): :meth:`EventBus.publish` may be called from *any* thread, while
subscribers are asyncio queues owned by the daemon loop.  Publishing therefore
hops onto the captured loop with ``loop.call_soon_threadsafe`` instead of
touching subscriber queues directly.

Slow consumers are not allowed to stall the daemon: every subscriber owns a
bounded ``asyncio.Queue`` and when it is full the *oldest* event is dropped.
This matches the "observation stream" role of ``/v1/events`` (HANDOVER §11.4:
consumers are humans/CLIs, not backends), so losing old events is acceptable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any, Final

__all__ = ["QUEUE_MAXSIZE", "EventBus"]

#: Per-subscriber backlog.  Full queues drop their oldest event.
QUEUE_MAXSIZE: Final[int] = 256

logger = logging.getLogger(__name__)


class EventBus:
    """Thread-safe, coroutine-safe, in-process fan-out bus.

    One instance is owned by :class:`~im_gateway.gateway.runtime.GatewayRuntime`
    and shared by every producer (channel state changes, push results, ...) and
    every ``/v1/events`` subscriber.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- loop binding ----------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Capture the daemon loop so thread producers can hop onto it.

        Called from the runtime's ``start()``; falls back to the running loop
        when one can be detected, and to direct delivery otherwise.
        """
        self._loop = loop if loop is not None else asyncio.get_running_loop()

    @property
    def subscriber_count(self) -> int:
        """Number of currently attached subscribers (diagnostics/tests)."""
        with self._lock:
            return len(self._subscribers)

    # -- producer side ---------------------------------------------------------

    def publish(self, event: dict[str, Any]) -> None:
        """Broadcast one event; safe to call from any thread."""
        with self._lock:
            subscribers = tuple(self._subscribers)
        if not subscribers:
            return

        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._deliver, subscribers, dict(event))
                return
            except RuntimeError:  # loop closed between the check and the call
                logger.debug("event bus loop is closed; dropping event")

        self._deliver(subscribers, dict(event))

    @staticmethod
    def _deliver(
        subscribers: tuple[asyncio.Queue[dict[str, Any]], ...],
        event: dict[str, Any],
    ) -> None:
        """Run on the loop thread: push to each subscriber, dropping the oldest."""
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    # -- consumer side ---------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events until the caller (or the client) goes away.

        Each call gets an independent queue, so multiple SSE clients are
        isolated from each other and from the daemon.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                self._subscribers.discard(queue)
