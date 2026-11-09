"""Milestone-1 placeholder processor: echo every inbound message.

``octop-gateway`` calls ``processor(message)`` and immediately ``async for``-s
the result (HANDOVER §11.2 #1), so this **must** be an async generator function
— an ``async def`` returning an async iterator would raise ``TypeError``.

Milestone 2 replaces this with the real model-A processor: persist the inbound
message, POST a ``TurnFrame`` (``kind=start``) to the tenant backend, translate
the backend's SSE frame stream into ``MessageEvent`` objects, and always emit a
terminal ``completed``/``error`` event (HANDOVER §11.2 #2/#3 — the upstream
swallows processor exceptions, so error reporting must happen in-band).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from octop_gateway.models import InboundMessage, MessageEvent

__all__ = ["echo_processor"]


async def echo_processor(message: InboundMessage) -> AsyncIterator[MessageEvent]:
    """Yield the user's own text back through the channel."""
    yield MessageEvent.typing()
    text = message.text
    yield MessageEvent.text(f"echo: {text}" if text else "echo: (no text)")
    yield MessageEvent.completed()
