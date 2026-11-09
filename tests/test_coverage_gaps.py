"""Targeted tests for the remaining defensive/edge branches (lead review).

These cover code paths that are hard to reach from the main suites: retention
knob coercion, uniqueness races, processor failure isolation, SSE grammar
corners, login adapter guards and the retention loop's error handling.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from octop_gateway.models import ChannelSubject, InboundMessage, TextContent

from im_gateway.core.contracts import (
    FrameKind,
    TurnFrame,
    frame_to_message_event,
    start_from_inbound,
)
from im_gateway.db.models import BackendRecord, ChannelRecord
from im_gateway.db.store import Store
from im_gateway.gateway.login import LoginManager, QQLoginAdapter, WeixinLoginAdapter
from im_gateway.gateway.maintenance import RetentionScheduler
from im_gateway.gateway.processor import BackendRouter, TurnProcessor, build_processor

# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


def test_frame_to_message_event_helper() -> None:
    event = frame_to_message_event(TurnFrame(turn_id="t", seq=1, kind=FrameKind.COMPLETED))
    assert event.type.value == "completed"


def test_start_from_inbound_requires_subject() -> None:
    message = InboundMessage(
        channel_id="feishu:acme:1",
        channel_type="feishu",
        content=[TextContent(text="hi")],
    )
    with pytest.raises(ValueError, match="channel_subject"):
        start_from_inbound(
            message,
            session_key="s",
            idempotency_key="k",
            deadline_ms=1000,
            turn_id="t",
        )


# ---------------------------------------------------------------------------
# Store: retention coercion + uniqueness races
# ---------------------------------------------------------------------------


def test_prune_ignores_malformed_retention_knobs(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    now = time.time()
    store.add_audit("x", actor="test")
    # Non-numeric / negative knobs mean "leave alone"; bools are rejected too.
    deleted = store.prune(
        {
            "chat_days": "nope",
            "audit_days": -1,
            "dedup_days": True,
            "turn_events_days": None,
            "chat_max_rows": "many",
        },
        now=now,
    )
    assert deleted["audit"] == 0
    assert store.table_counts()["audit"] == 1
    # chat_max_rows non-numeric => no cap; day knobs missing => nothing deleted.
    assert deleted["messages"] == 0


def test_add_channel_duplicate_alias_raises(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    now = time.time()

    def record(channel_id: str, alias: str) -> ChannelRecord:
        return ChannelRecord(
            channel_id=channel_id,
            kind="feishu",
            tenant_id="acme",
            alias=alias,
            name="",
            config={},
            constraints={},
            enabled=False,
            created_at=now,
            updated_at=now,
        )

    store.add_channel(record("a", "shared"))
    with pytest.raises(ValueError, match="alias already exists"):
        store.add_channel(record("b", "shared"))


def test_update_channel_duplicate_alias_raises(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    now = time.time()
    for channel_id, alias in (("a", "one"), ("b", "two")):
        store.add_channel(
            ChannelRecord(
                channel_id=channel_id,
                kind="feishu",
                tenant_id="acme",
                alias=alias,
                name="",
                config={},
                constraints={},
                enabled=False,
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(ValueError, match="alias already exists"):
        store.update_channel("b", alias="one")


# ---------------------------------------------------------------------------
# Processor: defaults, SSE grammar corners, failure isolation
# ---------------------------------------------------------------------------


def _store_with_backend(db_path: Path) -> Store:
    store = Store(db_path)
    store.migrate()
    now = time.time()
    store.upsert_backend(
        BackendRecord(
            tenant_id="",
            base_url="http://backend.test",
            token_ref="tok",
            timeout_ms=2000,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    return store


def _message() -> InboundMessage:
    return InboundMessage(
        channel_id="feishu:acme:1",
        channel_type="feishu",
        tenant_id="acme",
        channel_subject=ChannelSubject(subject_id="ou_1", chat_type="direct"),
        content=[TextContent(text="hi")],
        metadata={"message_id": "om_1"},
        timestamp=1000.0,
    )


def _processor(store: Store, body: str) -> TurnProcessor:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    return TurnProcessor(
        store,
        BackendRouter(store),
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )


def test_new_async_client_factory() -> None:
    from im_gateway.gateway.processor import _new_async_client

    client = _new_async_client()
    assert isinstance(client, httpx.AsyncClient)
    asyncio.run(client.aclose())


def test_build_processor_ignores_bad_deadline_setting(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    store.set_setting("turn_deadline_ms", "not-a-number")
    store.set_setting("error_texts", {"backend_down": "自定义"})

    processor = build_processor(store)

    assert processor._default_deadline_ms == 120_000
    assert processor._error_texts["backend_down"] == "自定义"


async def _collect(processor: TurnProcessor, message: InboundMessage) -> list[Any]:
    return [event async for event in processor(message)]


async def test_generic_turn_failure_is_reported_in_band(db_path: Path) -> None:
    store = _store_with_backend(db_path)
    processor = _processor(store, "")

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("store exploded")

    store.claim_inbound = boom  # type: ignore[method-assign]

    events = await _collect(processor, _message())

    assert [event.type.value for event in events] == ["error"]
    assert "后端处理失败" in (events[0].error or "")


async def test_outbound_record_failure_does_not_break_turn(db_path: Path) -> None:
    store = _store_with_backend(db_path)
    body = 'data: {"turn_id":"t","seq":1,"kind":"completed"}\n\n'
    processor = _processor(store, body)
    original_add_message = store.add_message

    def flaky_add(record: Any) -> int:
        if getattr(record, "direction", "") == "out":
            raise RuntimeError("no history for you")
        return original_add_message(record)

    store.add_message = flaky_add  # type: ignore[method-assign]

    events = await _collect(processor, _message())

    assert [event.type.value for event in events] == ["completed"]


async def test_error_record_failure_still_returns_error_event(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()  # no backend configured -> backend_down path
    original_add_message = store.add_message

    def flaky_add(record: Any) -> int:
        if getattr(record, "direction", "") == "out":
            raise RuntimeError("no history for you")
        return original_add_message(record)

    store.add_message = flaky_add  # type: ignore[method-assign]
    processor = TurnProcessor(store, BackendRouter(store))

    events = await _collect(processor, _message())

    assert [event.type.value for event in events] == ["error"]
    assert "后端暂不可用" in (events[0].error or "")


async def test_event_publish_failure_does_not_break_turn(db_path: Path) -> None:
    store = _store_with_backend(db_path)
    body = 'data: {"turn_id":"t","seq":1,"kind":"completed"}\n\n'

    class BrokenBus:
        def publish(self, event: dict[str, Any]) -> None:
            raise RuntimeError("bus down")

    processor = TurnProcessor(
        store,
        BackendRouter(store),
        events=BrokenBus(),  # type: ignore[arg-type]
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text=body, headers={"content-type": "text/event-stream"}
                )
            )
        ),
    )

    events = await _collect(processor, _message())
    assert [event.type.value for event in events] == ["completed"]


async def test_sse_grammar_ignores_comments_and_joins_data(db_path: Path) -> None:
    store = _store_with_backend(db_path)
    body = (
        ": keep-alive\n"
        "event: message\n"
        "id: 7\n"
        "retry: 500\n"
        'data: {"turn_id":"t","seq":1,"kind":"delta",\n'
        'data: "content":[{"type":"text","text":"a"}]}\n'
        "\n"
        "unknown-field: x\n"
        'data: {"turn_id":"t","seq":2,"kind":"completed"}'  # no trailing blank line
    )
    processor = _processor(store, body)

    events = await _collect(processor, _message())

    assert [event.type.value for event in events] == ["delta", "completed"]


# ---------------------------------------------------------------------------
# Login adapters: guards + lazy default factories
# ---------------------------------------------------------------------------


async def test_qq_adapter_guards() -> None:
    adapter = QQLoginAdapter()
    assert (await adapter.poll()).status == "expired"
    await adapter.cancel()  # no upstream instance yet: must not raise


class FakeQQLogin:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def fetch_qr_code(self) -> Any:
        return type("QR", (), {"task_id": "task-2", "qrcode_url": "https://qr/qq2"})()

    async def poll(self, task_id: str) -> Any:
        return type("Poll", (), {"status": "pending", "credentials": []})()

    def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)


async def test_qq_adapter_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import octop_gateway.channels.qq.login_qr as qq_module

    monkeypatch.setattr(qq_module, "QQBotQRLogin", FakeQQLogin)
    adapter = QQLoginAdapter()
    challenge = await adapter.start()
    assert challenge.qr_content == "https://qr/qq2"
    assert (await adapter.poll()).status == "pending"
    await adapter.cancel()


async def test_weixin_adapter_guards() -> None:
    adapter = WeixinLoginAdapter()
    # poll before start: the task is None -> pending, not a crash
    assert (await adapter.poll()).status == "pending"
    await adapter.cancel()  # no task: no-op

    # wait task started without a login object -> expired result
    bare = WeixinLoginAdapter()
    await bare._wait_for_login()
    assert (await bare.poll()).status == "expired"

    # task finished without setting a result -> expired
    done_adapter = WeixinLoginAdapter()
    finished = asyncio.create_task(asyncio.sleep(0))
    done_adapter._task = finished
    await finished
    assert (await done_adapter.poll()).status == "expired"


async def test_weixin_adapter_cancel_pending_task() -> None:
    adapter = WeixinLoginAdapter()
    gate = asyncio.Event()

    async def pending() -> None:
        await gate.wait()

    adapter._task = asyncio.create_task(pending())
    await asyncio.sleep(0)

    await adapter.cancel()

    assert adapter._task is None


class FakeWeixinLogin:
    async def fetch_qr_code(self) -> Any:
        return type("QR", (), {"qrcode": "q", "qrcode_img_content": "https://qr/img2"})()

    async def wait_for_login(self, qrcode: str, on_scanned: Any = None) -> Any:
        return type(
            "Wait",
            (),
            {
                "connected": True,
                "bot_token": "bot",
                "account_id": "a2",
                "base_url": "https://base2",
                "message": "",
            },
        )()


async def test_weixin_adapter_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import octop_gateway.channels.weixin.login_qr as weixin_module

    monkeypatch.setattr(weixin_module, "WeixinQRLogin", FakeWeixinLogin)
    adapter = WeixinLoginAdapter()
    challenge = await adapter.start()
    assert challenge.qr_image_url == "https://qr/img2"
    await asyncio.sleep(0)
    assert (await adapter.poll()).status == "success"
    await adapter.cancel()


def test_merge_accounts_skips_non_objects() -> None:
    manager = LoginManager(object())  # type: ignore[arg-type]
    merged = manager._merge_accounts(["legacy"], ["nope", {"account_id": "a"}])
    assert merged == ["legacy", {"account_id": "a"}]


# ---------------------------------------------------------------------------
# Retention loop: a failed pass is logged, the scheduler keeps running
# ---------------------------------------------------------------------------


class _FakeStore:
    def get_setting(self, key: str, default: Any = None) -> Any:
        return default

    def prune(self, retention: dict[str, Any], *, now: float) -> dict[str, int]:
        raise RuntimeError("prune exploded")


async def test_retention_loop_survives_failed_pass() -> None:
    async def sleeper(_seconds: float) -> None:
        await asyncio.sleep(0)

    scheduler = RetentionScheduler(
        _FakeStore(),  # type: ignore[arg-type]
        interval_s=0.001,
        sleep=sleeper,
    )
    await scheduler.start()
    await asyncio.sleep(0.02)

    assert scheduler.running is True
    assert scheduler.last_result == {}

    await scheduler.stop()
    assert scheduler.running is False
