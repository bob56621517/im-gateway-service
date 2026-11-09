"""Lifecycle tests: graceful shutdown and retention scheduling wiring.

These exercise **our** runtime/app code with a fake upstream manager; the real
octop-gateway manager is never started and no network is touched.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from im_gateway.db.models import MessageRecord
from im_gateway.db.store import Store
from im_gateway.gateway.app import create_app
from im_gateway.gateway.runtime import GatewayRuntime

TOKEN = "lifecycle-token"


class FakeManager:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class FakeProcessor:
    """Only the surface the runtime touches: an ``active_turns`` attribute."""

    def __init__(self, active: int = 0) -> None:
        self.active_turns = active


def make_runtime(store: Store, manager: FakeManager, processor: Any) -> GatewayRuntime:
    return GatewayRuntime(store, token=TOKEN, manager=manager, processor=processor)


async def test_stop_waits_for_active_turns(db_path: Path) -> None:
    store = Store(db_path)
    manager = FakeManager()
    processor = FakeProcessor(active=1)
    runtime = make_runtime(store, manager, processor)
    await runtime.start()

    async def finish_turn() -> None:
        await asyncio.sleep(0.12)
        processor.active_turns = 0

    finisher = asyncio.create_task(finish_turn())
    started = time.monotonic()
    await runtime.stop()
    elapsed = time.monotonic() - started
    await finisher

    assert elapsed >= 0.1, "shutdown must wait for the in-flight turn"
    assert manager.stopped == 1


async def test_stop_expires_after_grace(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    store.set_setting("shutdown_grace_ms", 120)
    manager = FakeManager()
    processor = FakeProcessor(active=1)
    runtime = make_runtime(store, manager, processor)
    await runtime.start()

    started = time.monotonic()
    await runtime.stop()
    elapsed = time.monotonic() - started

    assert 0.1 <= elapsed < 1.0
    assert manager.stopped == 1


async def test_stop_is_idempotent(db_path: Path) -> None:
    store = Store(db_path)
    manager = FakeManager()
    runtime = make_runtime(store, manager, FakeProcessor(active=0))
    await runtime.start()
    await runtime.stop()
    await runtime.stop()
    assert manager.stopped == 1


async def test_wait_for_idle_returns_immediately_when_idle(db_path: Path) -> None:
    store = Store(db_path)
    runtime = make_runtime(store, FakeManager(), FakeProcessor(active=0))
    started = time.monotonic()
    assert await runtime.wait_for_idle_turns(5.0) is True
    assert time.monotonic() - started < 0.2


async def test_active_turns_defaults_to_zero_for_plain_processors(db_path: Path) -> None:
    store = Store(db_path)
    runtime = make_runtime(store, FakeManager(), object())
    assert runtime.active_turns == 0


async def test_active_turns_survives_garbage_values(db_path: Path) -> None:
    store = Store(db_path)

    class Weird:
        active_turns = "not-a-number"

    runtime = make_runtime(store, FakeManager(), Weird())
    assert runtime.active_turns == 0


def test_lifespan_starts_and_stops_retention_scheduler(db_path: Path) -> None:
    store = Store(db_path)
    app = create_app(store=store, token=TOKEN, manager=FakeManager())
    with _ClientProxy(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.scheduler.running is True
    assert app.state.scheduler.running is False


def test_lifespan_catch_up_prunes_old_messages(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    store.add_message(
        MessageRecord(
            channel_id="feishu:acme:sales",
            subject_id="ou_1",
            session_key="feishu:acme:sales:ou_1",
            direction="in",
            kind="inbound",
            content=[{"type": "text", "text": "ancient"}],
            metadata={},
            platform_msg_id="om_old",
            ts=0.0,
            error=None,
        )
    )
    assert store.table_counts()["messages"] == 1

    app = create_app(store=store, token=TOKEN, manager=FakeManager())
    with _ClientProxy(app) as client:
        assert client.get("/healthz").status_code == 200
        # The startup catch-up pass prunes rows older than retention.chat_days.
        assert store.table_counts()["messages"] == 0


class _ClientProxy:
    """Thin wrapper so the module header stays free of testclient imports."""

    def __init__(self, app: Any) -> None:
        import warnings

        from fastapi.testclient import TestClient

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._client = TestClient(app)

    def __enter__(self) -> Any:
        return self._client.__enter__()

    def __exit__(self, *exc_info: object) -> Any:
        return self._client.__exit__(*exc_info)

async def test_default_processor_is_built_after_migration(db_path: Path) -> None:
    """A runtime created before the DB exists must still boot (settings read late)."""
    from im_gateway.gateway.processor import TurnProcessor

    store = Store(db_path)  # deliberately NOT migrated yet
    runtime = make_runtime(store, FakeManager(), processor=None)  # type: ignore[arg-type]
    await runtime.start()

    assert isinstance(runtime.processor, TurnProcessor)
    assert store.table_counts()["channels"] == 0
    await runtime.stop()
