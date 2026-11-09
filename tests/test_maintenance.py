"""Tests for the retention scheduler (our code only; the store is faked)."""

from __future__ import annotations

import asyncio
from typing import Any

from im_gateway.gateway.maintenance import (
    DEFAULT_INTERVAL_S,
    DEFAULT_RETENTION,
    RetentionScheduler,
    merge_retention,
)


class FakeStore:
    """Minimal store double: only the two calls the scheduler makes."""

    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        setting_error: Exception | None = None,
    ) -> None:
        self.settings = settings or {}
        self.setting_error = setting_error
        self.get_setting_calls: list[str] = []

    def get_setting(self, key: str, default: Any = None) -> Any:
        self.get_setting_calls.append(key)
        if self.setting_error is not None:
            raise self.setting_error
        return self.settings.get(key, default)

    def prune(self, retention: dict[str, Any], *, now: float) -> dict[str, int]:
        """Default ``Store.prune`` stand-in (only used when no fake is injected)."""
        return {}


class RecordingPrune:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], float]] = []

    def __call__(self, retention: dict[str, Any], *, now: float) -> dict[str, int]:
        self.calls.append((dict(retention), now))
        return {"messages": 3, "audit": 1, "inbound_dedup": 2, "turn_events": 0}


def test_merge_retention_defaults_and_override() -> None:
    assert merge_retention({}) == DEFAULT_RETENTION
    merged = merge_retention({"chat_days": 7})
    assert merged["chat_days"] == 7
    assert merged["audit_days"] == DEFAULT_RETENTION["audit_days"]
    # A malformed settings value must not crash the daemon.
    assert merge_retention("nope") == DEFAULT_RETENTION
    assert merge_retention(None) == DEFAULT_RETENTION


async def test_run_once_uses_defaults_when_no_settings() -> None:
    store = FakeStore()
    prune = RecordingPrune()
    scheduler = RetentionScheduler(store, clock=lambda: 1000.0, prune=prune)  # type: ignore[arg-type]

    deleted = await scheduler.run_once()

    assert deleted == {"messages": 3, "audit": 1, "inbound_dedup": 2, "turn_events": 0}
    assert scheduler.last_result == deleted
    assert scheduler.last_run_at == 1000.0
    assert prune.calls == [(DEFAULT_RETENTION, 1000.0)]
    assert store.get_setting_calls == ["retention"]


async def test_run_once_merges_settings_over_defaults() -> None:
    store = FakeStore(settings={"retention": {"chat_days": 1, "chat_max_rows": 10}})
    prune = RecordingPrune()
    scheduler = RetentionScheduler(store, clock=lambda: 42.0, prune=prune)  # type: ignore[arg-type]

    await scheduler.run_once()

    retention, now = prune.calls[0]
    assert now == 42.0
    assert retention["chat_days"] == 1
    assert retention["chat_max_rows"] == 10
    assert retention["audit_days"] == DEFAULT_RETENTION["audit_days"]


async def test_run_once_survives_unreadable_settings() -> None:
    store = FakeStore(setting_error=RuntimeError("db gone"))
    prune = RecordingPrune()
    scheduler = RetentionScheduler(store, prune=prune)  # type: ignore[arg-type]

    await scheduler.run_once()

    assert prune.calls[0][0] == DEFAULT_RETENTION


async def test_default_interval_is_one_day() -> None:
    scheduler = RetentionScheduler(FakeStore())  # type: ignore[arg-type]
    assert scheduler.interval_s == DEFAULT_INTERVAL_S
    assert scheduler.running is False


async def test_start_runs_catch_up_then_loop_and_stop_cancels() -> None:
    store = FakeStore()
    prune = RecordingPrune()
    gate = asyncio.Event()

    async def sleeper(_seconds: float) -> None:
        await gate.wait()
        gate.clear()

    scheduler = RetentionScheduler(store, interval_s=0.001, sleep=sleeper, prune=prune)  # type: ignore[arg-type]

    await scheduler.start()
    assert scheduler.running is True
    assert len(prune.calls) == 1  # catch-up pass

    await scheduler.start()  # idempotent
    assert len(prune.calls) == 1

    gate.set()  # let the loop run exactly one periodic pass
    await asyncio.sleep(0.05)
    assert len(prune.calls) == 2

    await scheduler.stop()
    assert scheduler.running is False


async def test_stop_is_idempotent_and_safe_without_start() -> None:
    scheduler = RetentionScheduler(FakeStore())  # type: ignore[arg-type]
    await scheduler.stop()
    await scheduler.stop()
    assert scheduler.running is False


async def test_catch_up_failure_does_not_raise() -> None:
    store = FakeStore()
    gate = asyncio.Event()

    async def sleeper(_seconds: float) -> None:
        await gate.wait()

    def broken_prune(retention: dict[str, Any], *, now: float) -> dict[str, int]:
        raise RuntimeError("prune failed")

    scheduler = RetentionScheduler(
        store,  # type: ignore[arg-type]
        interval_s=0.001,
        sleep=sleeper,
        prune=broken_prune,
    )
    await scheduler.start()
    assert scheduler.running is True
    assert scheduler.last_result == {}
    await scheduler.stop()
