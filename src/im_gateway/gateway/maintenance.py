"""Retention scheduler for runtime data (HANDOVER §13.9).

The daemon runs the retention pass once at startup (catch-up for the time it was
down) and then once per interval.  The scheduler owns no database logic: it only
merges ``settings.retention`` over the defaults and calls ``Store.prune``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from im_gateway.db.store import Store

__all__ = ["DEFAULT_INTERVAL_S", "DEFAULT_RETENTION", "RetentionScheduler"]

logger = logging.getLogger(__name__)

#: One day.  The scheduler is not precision-critical; startup does a catch-up run.
DEFAULT_INTERVAL_S: Final[float] = 86_400.0

#: Defaults for ``settings.retention`` (see HANDOVER §13.9).
DEFAULT_RETENTION: Final[dict[str, Any]] = {
    "chat_days": 30,
    "chat_max_rows": 500_000,
    "turn_events_days": 1,
    "dedup_days": 7,
    "audit_days": 90,
    "media_days": 30,
}

PruneFn = Callable[..., dict[str, int]]


def merge_retention(raw: Any) -> dict[str, Any]:
    """Merge ``settings.retention`` over :data:`DEFAULT_RETENTION`.

    Unknown keys are passed through (the store ignores what it does not know);
    a non-dict value falls back to the defaults instead of crashing the daemon.
    """
    merged = dict(DEFAULT_RETENTION)
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


class RetentionScheduler:
    """Runs ``Store.prune`` on a fixed interval with injectable clock/sleep."""

    def __init__(
        self,
        store: Store,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        prune: PruneFn | None = None,
    ) -> None:
        self._store = store
        self._interval_s = max(0.001, float(interval_s))
        self._clock = clock
        self._sleep = sleep
        self._prune: PruneFn = prune if prune is not None else store.prune
        self._task: asyncio.Task[None] | None = None
        #: Result of the most recent successful pass (diagnostics/tests).
        self.last_result: dict[str, int] = {}
        self.last_run_at: float | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def interval_s(self) -> float:
        return self._interval_s

    def retention_from_settings(self) -> dict[str, Any]:
        try:
            raw = self._store.get_setting("retention", {})
        except Exception:  # a broken settings row must not stop retention
            logger.exception("failed to read retention settings; using defaults")
            raw = {}
        return merge_retention(raw)

    async def run_once(self) -> dict[str, int]:
        """Run one pruning pass; returns per-table deleted counts."""
        retention = self.retention_from_settings()
        deleted = await asyncio.to_thread(self._prune, retention, now=self._clock())
        self.last_result = dict(deleted)
        self.last_run_at = self._clock()
        logger.info("retention pass finished: %s", deleted)
        return self.last_result

    async def start(self) -> None:
        """Run the catch-up pass and start the periodic loop (idempotent)."""
        if self.running:
            return
        # Catch up first so a long downtime does not delay pruning by a day.
        try:
            await self.run_once()
        except Exception:  # startup must not be blocked by a failing prune
            logger.exception("initial retention pass failed")
        self._task = asyncio.create_task(self._loop(), name="imgw-retention")

    async def stop(self) -> None:
        """Cancel the periodic loop (idempotent)."""
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        try:
            while True:
                await self._sleep(self._interval_s)
                try:
                    await self.run_once()
                except Exception:  # keep the scheduler alive across failed passes
                    logger.exception("retention pass failed")
        except asyncio.CancelledError:
            logger.debug("retention scheduler cancelled")
