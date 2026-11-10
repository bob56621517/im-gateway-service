"""Tests for the model-A turn processor (HANDOVER §12, §16) and dedup keys.

No network, no platform SDKs: the backend is an :class:`httpx.MockTransport`
injected through ``http_client_factory`` and every test gets its own temporary
SQLite database.

Backend configuration is the version-2 model: one URL, one optional token, one
timeout (CLI flag > environment > default). There is no tenant/backend table.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from octop_gateway.models import ChannelSubject, InboundMessage, MessageEvent, MessageEventType, TextContent

from im_gateway.db.models import ChannelRecord, SubjectRecord
from im_gateway.db.store import Store
from im_gateway.gateway.idempotency import build_idempotency_key, extract_platform_message_id
from im_gateway.gateway.processor import (
    DEFAULT_BACKEND_TIMEOUT_MS,
    DEFAULT_BACKEND_URL,
    DEFAULT_ERROR_TEXTS,
    BackendSpec,
    TurnProcessor,
    build_processor,
    resolve_backend_spec,
    resolve_backend_url,
)

BASE_URL = "http://backend.test"
TOKEN = "backend-token"
BACKEND_TURN_ID = "backend-turn"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(db_path: Path) -> Store:
    store = Store(db_path)
    store.migrate()
    return store


def add_channel_record(
    store: Store,
    *,
    channel_id: str = "feishu:acme:sales",
    kind: str = "feishu",
    name: str = "Sales",
    alias: str | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    now = time.time()
    store.add_channel(
        ChannelRecord(
            channel_id=channel_id,
            kind=kind,
            alias=alias,
            name=name,
            config=dict(config or {}),
            constraints={},
            enabled=True,
            created_at=now,
            updated_at=now,
        )
    )


def default_spec(timeout_ms: int = 3000) -> BackendSpec:
    return BackendSpec(base_url=BASE_URL, token=TOKEN, timeout_ms=timeout_ms)


def make_message(
    *,
    channel_id: str = "feishu:acme:sales",
    channel_type: str = "feishu",
    tenant_id: str | None = "acme",
    subject_id: str = "ou_1",
    text: str = "你好",
    metadata: dict[str, Any] | None = None,
    timestamp: float = 1000.0,
) -> InboundMessage:
    return InboundMessage(
        channel_id=channel_id,
        channel_type=channel_type,
        tenant_id=tenant_id,
        channel_subject=ChannelSubject(
            subject_id=subject_id,
            chat_type="direct",
            display_name="张三",
        ),
        content=[TextContent(text=text)],
        metadata=dict(metadata or {}),
        timestamp=timestamp,
    )


class FakeBackend:
    """An httpx transport that records requests and runs a handler."""

    def __init__(self, handler: Callable[[httpx.Request], Any]) -> None:
        self.calls: list[httpx.Request] = []
        self._handler = handler
        self.transport = httpx.MockTransport(self._dispatch)

    async def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        result = self._handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result

    def client_factory(self) -> Callable[[], httpx.AsyncClient]:
        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=self.transport)

        return factory


def make_processor(
    store: Store,
    backend: FakeBackend,
    *,
    spec: BackendSpec | None = None,
    **kwargs: Any,
) -> TurnProcessor:
    return TurnProcessor(
        store,
        spec if spec is not None else default_spec(),
        http_client_factory=backend.client_factory(),
        **kwargs,
    )


async def collect(processor: TurnProcessor, message: InboundMessage) -> list[MessageEvent]:
    return [event async for event in processor(message)]


def event_types(events: list[MessageEvent]) -> list[MessageEventType]:
    return [event.type for event in events]


def frame(seq: int, kind: str, **fields: Any) -> dict[str, Any]:
    return {"turn_id": BACKEND_TURN_ID, "seq": seq, "kind": kind, **fields}


def text_frame(seq: int, kind: str, text: str) -> dict[str, Any]:
    return frame(seq, kind, content=[{"type": "text", "text": text}])


def start_payload(seq: int = 0) -> dict[str, Any]:
    return {
        "turn_id": BACKEND_TURN_ID,
        "seq": seq,
        "kind": "start",
        "channel": {"channel_id": "feishu:acme:sales", "kind": "feishu", "tenant_id": ""},
        "subject": {"subject_id": "ou_1"},
        "session_key": "ou_1",
        "idempotency_key": "feishu:acme:sales:om_x",
        "deadline_ms": 3000,
        "content": [{"type": "text", "text": "hi"}],
        "capabilities": {"streaming": "buffered"},
    }


def sse_body(*frames: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(item, ensure_ascii=False)}\n\n" for item in frames)


def sse_response(*frames: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=sse_body(*frames).encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


def raw_sse_response(body: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


def static_handler(*frames: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(*frames)

    return handler


# ---------------------------------------------------------------------------
# Backend URL / token / timeout resolution (no tenant/backend table)
# ---------------------------------------------------------------------------


def test_resolve_backend_spec_defaults() -> None:
    spec = resolve_backend_spec(env={})

    assert spec == BackendSpec(
        base_url=DEFAULT_BACKEND_URL,
        token="",
        timeout_ms=DEFAULT_BACKEND_TIMEOUT_MS,
    )
    assert DEFAULT_BACKEND_URL == "http://127.0.0.1:8080"
    assert DEFAULT_BACKEND_TIMEOUT_MS == 120_000


def test_resolve_backend_spec_env_and_flag_priority() -> None:
    env: Mapping[str, str] = {
        "IMGW_BACKEND_URL": "http://env.test/",
        "IMGW_BACKEND_TOKEN": "env-token",
        "IMGW_BACKEND_TIMEOUT_MS": "5000",
    }
    from_env = resolve_backend_spec(env=env)
    assert from_env.base_url == "http://env.test"
    assert from_env.token == "env-token"
    assert from_env.timeout_ms == 5000

    # The explicit flag (imgw run --backend-url) wins over the environment.
    explicit = resolve_backend_spec(backend_url="http://cli.test/", env=env)
    assert explicit.base_url == "http://cli.test"


@pytest.mark.parametrize("raw", ["", "  ", "not-a-number", "0", "-5"])
def test_resolve_backend_spec_falls_back_on_bad_timeout(raw: str) -> None:
    spec = resolve_backend_spec(env={"IMGW_BACKEND_TIMEOUT_MS": raw})
    assert spec.timeout_ms == DEFAULT_BACKEND_TIMEOUT_MS


def test_resolve_backend_url_seam() -> None:
    """Future seam: a plain per-channel config key may override the global URL."""
    assert resolve_backend_url(None, "http://global.test/") == "http://global.test"
    assert resolve_backend_url({}, "http://global.test") == "http://global.test"
    assert (
        resolve_backend_url({"backend_url": "http://channel.test/"}, "http://global.test")
        == "http://channel.test"
    )
    # Anything that is not a non-blank string falls back to the global URL.
    assert resolve_backend_url({"backend_url": ""}, "http://global.test") == "http://global.test"
    assert resolve_backend_url({"backend_url": 42}, "http://global.test") == "http://global.test"


# ---------------------------------------------------------------------------
# 1. Happy path: request frame, headers, SSE -> MessageEvent mapping, persistence
# ---------------------------------------------------------------------------


async def test_normal_turn_streams_events_and_persists(store: Store) -> None:
    add_channel_record(store, name="Sales")
    backend = FakeBackend(
        static_handler(
            frame(1, "typing"),
            text_frame(2, "delta", "你"),
            text_frame(3, "delta", "好"),
            frame(4, "completed"),
        )
    )
    message = make_message(metadata={"message_id": "om_1"})
    processor = build_processor(
        store,
        backend=default_spec(),
        http_client_factory=backend.client_factory(),
    )

    events = await collect(processor, message)

    assert event_types(events) == [
        MessageEventType.TYPING,
        MessageEventType.DELTA,
        MessageEventType.DELTA,
        MessageEventType.COMPLETED,
    ]
    assert events[1].content[0].text == "你"
    assert events[2].content[0].text == "好"
    assert processor.active_turns == 0

    # --- request frame / URL / distribution headers ---
    assert len(backend.calls) == 1
    request = backend.calls[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/v1/turns"
    assert request.url.path == "/v1/turns"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-imgw-channel-id"] == "feishu:acme:sales"
    assert request.headers["x-imgw-channel-name"] == "Sales"
    assert request.headers["x-imgw-kind"] == "feishu"
    body = json.loads(request.content)
    assert body["kind"] == "start"
    assert body["channel"] == {
        "channel_id": "feishu:acme:sales",
        "kind": "feishu",
        # Frozen wire field: tenants are gone, so it is always empty even though
        # the upstream InboundMessage still carries one.
        "tenant_id": "",
    }
    assert body["subject"]["subject_id"] == "ou_1"
    assert body["session_key"] == "ou_1"
    assert body["idempotency_key"] == "feishu:acme:sales:om_1"
    assert body["deadline_ms"] == 3000
    assert body["capabilities"]["streaming"] == "buffered"
    assert body["content"] == [{"type": "text", "text": "你好"}]
    turn_id = body["turn_id"]
    assert isinstance(turn_id, str)
    assert len(turn_id) == 32

    # --- inbound + outbound history ---
    rows = store.query_history(channel_id=message.channel_id, subject_id="ou_1")
    assert [row.direction for row in rows] == ["out", "in"]
    outbound, inbound = rows
    assert inbound.kind == "inbound"
    assert inbound.session_key == "ou_1"
    assert inbound.content == [{"type": "text", "text": "你好"}]
    assert inbound.platform_msg_id == "om_1"
    assert inbound.turn_id == turn_id
    assert outbound.kind == "message"
    assert outbound.content == [{"type": "text", "text": "你好"}]
    assert outbound.metadata == {"turn_id": turn_id}
    assert outbound.turn_id == turn_id
    assert outbound.ts > 0

    # --- subject upsert ---
    subject = store.get_subject(message.channel_id, "ou_1")
    assert subject is not None
    assert subject.chat_type == "direct"
    assert subject.display_name == "张三"
    assert subject.first_seen == message.timestamp
    assert subject.last_seen == message.timestamp

    # --- dedup claim ---
    assert store.claim_inbound("feishu:acme:sales:om_1", message.channel_id) is False


async def test_default_backend_url_is_used_without_any_config(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backend row/config: the default URL is used; nothing needs provisioning."""
    monkeypatch.delenv("IMGW_BACKEND_URL", raising=False)
    monkeypatch.delenv("IMGW_BACKEND_TOKEN", raising=False)
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = build_processor(store, http_client_factory=backend.client_factory())

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.COMPLETED]
    assert str(backend.calls[0].url) == f"{DEFAULT_BACKEND_URL}/v1/turns"
    assert "authorization" not in backend.calls[0].headers


async def test_channel_name_empty_omits_distribution_header(store: Store) -> None:
    add_channel_record(store, name="")
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = make_processor(store, backend)

    await collect(processor, make_message())

    assert backend.calls[0].headers["x-imgw-channel-id"] == "feishu:acme:sales"
    assert "x-imgw-channel-name" not in backend.calls[0].headers


async def test_per_channel_backend_url_seam_is_additive(store: Store) -> None:
    """A plain channel config key overrides the global URL (no CLI/help exposure)."""
    add_channel_record(store, config={"backend_url": "http://override.test/"})
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = make_processor(store, backend)

    await collect(processor, make_message())

    assert str(backend.calls[0].url) == "http://override.test/v1/turns"


# ---------------------------------------------------------------------------
# 2. Idempotency: a redelivered message does not call the backend twice
# ---------------------------------------------------------------------------


async def test_duplicate_message_yields_only_completed(store: Store) -> None:
    backend = FakeBackend(static_handler(frame(1, "completed")))
    message = make_message(metadata={"message_id": "om_dup"})
    processor = make_processor(store, backend)

    first = await collect(processor, message)
    second = await collect(processor, message)

    assert event_types(first) == [MessageEventType.COMPLETED]
    assert event_types(second) == [MessageEventType.COMPLETED]
    assert len(backend.calls) == 1
    assert processor.active_turns == 0


# ---------------------------------------------------------------------------
# 3. HTTP-level failures
# ---------------------------------------------------------------------------


async def test_http_error_status_yields_error_with_status_code(store: Store) -> None:
    backend = FakeBackend(lambda request: httpx.Response(500, json={"error": "boom"}))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error is not None
    assert DEFAULT_ERROR_TEXTS["backend_http"] in events[0].error
    assert "500" in events[0].error
    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert rows[0].kind == "error"
    assert rows[0].error == events[0].error


async def test_connect_error_maps_to_backend_down(store: Store) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = FakeBackend(handler)
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == DEFAULT_ERROR_TEXTS["backend_down"]


async def test_deadline_timeout_maps_to_backend_timeout(store: Store) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return sse_response(frame(1, "completed"))

    backend = FakeBackend(handler)
    processor = make_processor(store, backend, spec=default_spec(timeout_ms=100))

    started = time.monotonic()
    events = await collect(processor, make_message())
    elapsed = time.monotonic() - started

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == DEFAULT_ERROR_TEXTS["backend_timeout"]
    assert elapsed < 0.9
    assert processor.active_turns == 0


# ---------------------------------------------------------------------------
# 4. Broken frame streams
# ---------------------------------------------------------------------------


async def test_eof_without_terminal_frame_yields_incomplete(store: Store) -> None:
    backend = FakeBackend(static_handler(text_frame(1, "delta", "只说了一半")))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.DELTA, MessageEventType.ERROR]
    assert events[0].content[0].text == "只说了一半"
    assert events[1].error == DEFAULT_ERROR_TEXTS["backend_incomplete"]


async def test_invalid_json_frame_yields_error(store: Store) -> None:
    backend = FakeBackend(lambda request: raw_sse_response("data: this is not json\n\n"))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == "后端返回了无效帧"


async def test_invalid_frame_missing_required_field_yields_error(store: Store) -> None:
    backend = FakeBackend(lambda request: raw_sse_response('data: {"kind": "delta", "seq": 1}\n\n'))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == "后端返回了无效帧"


async def test_sequence_gap_yields_error(store: Store) -> None:
    backend = FakeBackend(
        static_handler(text_frame(1, "delta", "一"), frame(3, "completed"))
    )
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.DELTA, MessageEventType.ERROR]
    assert events[1].error == "后端帧序号不连续"


async def test_first_frame_sequence_must_be_positive(store: Store) -> None:
    backend = FakeBackend(static_handler(text_frame(0, "delta", "零")))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == "后端帧序号不连续"


# ---------------------------------------------------------------------------
# 5. Illegal frame kinds in a response
# ---------------------------------------------------------------------------


async def test_start_frame_in_response_is_protocol_error(store: Store) -> None:
    backend = FakeBackend(lambda request: raw_sse_response(sse_body(start_payload())))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error is not None
    assert "start" in events[0].error


async def test_native_frame_in_response_is_protocol_error(store: Store) -> None:
    native = frame(1, "native", native={"op": "feishu.card", "payload": {}})
    backend = FakeBackend(static_handler(native))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == "当前网关不支持原生能力"


# ---------------------------------------------------------------------------
# 6. ERROR frame is terminal: no COMPLETED after it
# ---------------------------------------------------------------------------


async def test_error_frame_is_terminal_and_not_followed_by_completed(store: Store) -> None:
    backend = FakeBackend(
        static_handler(
            text_frame(1, "delta", "部分"),
            frame(2, "error", error="后端出错了"),
            frame(3, "completed"),
        )
    )
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.DELTA, MessageEventType.ERROR]
    assert events[1].error == "后端出错了"
    assert MessageEventType.COMPLETED not in event_types(events)
    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert rows[0].direction == "out"
    assert rows[0].kind == "error"
    assert rows[0].error == "后端出错了"


# ---------------------------------------------------------------------------
# 7. Idempotency keys per channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel_type", "metadata", "expected_suffix"),
    [
        ("feishu", {"message_id": "om_1"}, "om_1"),
        ("telegram", {"message_id": "tg_1"}, "tg_1"),
        ("discord", {"message_id": "dc_1"}, "dc_1"),
        ("xiaoyi", {"message_id": "xy_1"}, "xy_1"),
        ("qq", {"msg_id": "qq_1"}, "qq_1"),
        ("dingtalk", {"msg_id": "dd_1"}, "dd_1"),
        ("yuanbao", {"msg_id": "yb_1"}, "yb_1"),
        ("weixin", {"_weixin_msg_id": "wx_1"}, "wx_1"),
    ],
)
def test_idempotency_key_uses_platform_message_id(
    channel_type: str, metadata: dict[str, str], expected_suffix: str
) -> None:
    message = make_message(
        channel_id=f"{channel_type}:acme:sales",
        channel_type=channel_type,
        metadata=metadata,
    )

    assert extract_platform_message_id(message) == expected_suffix
    assert build_idempotency_key(message) == f"{channel_type}:acme:sales:{expected_suffix}"


@pytest.mark.parametrize("channel_type", ["wecom", "mqtt"])
def test_idempotency_key_hash_fallback_without_platform_id(channel_type: str) -> None:
    channel_id = f"{channel_type}:acme:sales"
    message = make_message(channel_id=channel_id, channel_type=channel_type)

    assert extract_platform_message_id(message) is None
    key = build_idempotency_key(message)
    assert key.startswith(f"{channel_id}:h:")
    assert len(key.rsplit(":h:", 1)[1]) == 32
    # Same message twice -> same key (redelivery is a duplicate) ...
    assert build_idempotency_key(make_message(channel_id=channel_id, channel_type=channel_type)) == key
    # ... different text -> different key.
    other = make_message(channel_id=channel_id, channel_type=channel_type, text="另一句话")
    assert build_idempotency_key(other) != key


def test_idempotency_key_treats_blank_platform_id_as_missing() -> None:
    message = make_message(channel_type="feishu", metadata={"message_id": "   "})

    assert extract_platform_message_id(message) is None
    assert ":h:" in build_idempotency_key(message)


# ---------------------------------------------------------------------------
# 8. active_turns: visible while the backend request is in flight
# ---------------------------------------------------------------------------


async def test_active_turns_tracks_in_flight_turn(store: Store) -> None:
    request_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release.wait()
        return sse_response(frame(1, "completed"))

    processor = make_processor(store, FakeBackend(handler))
    message = make_message()

    async def consume() -> list[MessageEvent]:
        return await collect(processor, message)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(request_started.wait(), timeout=1)
    assert processor.active_turns == 1

    release.set()
    events = await asyncio.wait_for(task, timeout=2)

    assert event_types(events) == [MessageEventType.COMPLETED]
    assert processor.active_turns == 0


# ---------------------------------------------------------------------------
# 9. subject metadata: ephemeral push fields are stripped, durable kept
# ---------------------------------------------------------------------------


async def test_subject_metadata_strips_ephemeral_fields(store: Store) -> None:
    processor = make_processor(store, FakeBackend(static_handler(frame(1, "completed"))))
    metadata = {
        "msg_id": "m1",
        "response_url": "https://platform.example/callback",
        "context_token": "ephemeral-token",
        "weixin_context_token": "durable-token",
        "sender_id": "sender-1",
        "chat_type": "group",
    }
    message = make_message(
        channel_id="weixin:acme:bot",
        channel_type="weixin",
        metadata=metadata,
    )

    await collect(processor, message)

    subject = store.get_subject("weixin:acme:bot", "ou_1")
    assert subject is not None
    assert subject.metadata == {
        "weixin_context_token": "durable-token",
        "sender_id": "sender-1",
        "chat_type": "group",
    }
    # The raw inbound history keeps the platform metadata untouched.
    inbound = store.query_history(channel_id="weixin:acme:bot", subject_id="ou_1")[-1]
    assert inbound.metadata == metadata


async def test_subject_upsert_preserves_first_seen(store: Store) -> None:
    store.upsert_subject(
        SubjectRecord(
            channel_id="feishu:acme:sales",
            subject_id="ou_1",
            chat_type="group",
            display_name="旧名字",
            metadata={"old": True},
            first_seen=10.0,
            last_seen=11.0,
        )
    )
    processor = make_processor(store, FakeBackend(static_handler(frame(1, "completed"))))

    await collect(processor, make_message(timestamp=1000.0))

    subject = store.get_subject("feishu:acme:sales", "ou_1")
    assert subject is not None
    assert subject.first_seen == 10.0
    assert subject.last_seen == 1000.0
    assert subject.metadata == {}


# ---------------------------------------------------------------------------
# 10. Settings-driven wiring (build_processor)
# ---------------------------------------------------------------------------


async def test_build_processor_merges_error_text_settings(store: Store) -> None:
    store.set_setting("error_texts", {"backend_down": "自定义不可用"})

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    backend = FakeBackend(handler)
    processor = build_processor(
        store,
        backend=default_spec(),
        http_client_factory=backend.client_factory(),
    )

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == "自定义不可用"


async def test_build_processor_uses_env_backend(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMGW_BACKEND_URL", "http://env.test/")
    monkeypatch.setenv("IMGW_BACKEND_TOKEN", "env-token")
    monkeypatch.setenv("IMGW_BACKEND_TIMEOUT_MS", "4321")
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = build_processor(store, http_client_factory=backend.client_factory())

    await collect(processor, make_message())

    request = backend.calls[0]
    assert str(request.url) == "http://env.test/v1/turns"
    assert request.headers["authorization"] == "Bearer env-token"
    assert json.loads(request.content)["deadline_ms"] == 4321


# ---------------------------------------------------------------------------
# 11. Message-level behaviour and parser details
# ---------------------------------------------------------------------------


async def test_missing_channel_subject_yields_error(store: Store) -> None:
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = make_processor(store, backend)
    message = InboundMessage(
        channel_id="feishu:acme:sales",
        channel_type="feishu",
        content=[TextContent(text="没有会话目标")],
    )

    events = await collect(processor, message)

    assert event_types(events) == [MessageEventType.ERROR]
    assert events[0].error == "消息缺少会话目标"
    assert backend.calls == []
    assert processor.active_turns == 0
    assert store.table_counts()["inbound_dedup"] == 0
    assert store.table_counts()["messages"] == 0


async def test_message_frame_is_forwarded_and_persisted(store: Store) -> None:
    backend = FakeBackend(
        static_handler(
            text_frame(1, "delta", "先说的"),
            text_frame(2, "message", "完整消息"),
            frame(3, "completed"),
        )
    )
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [
        MessageEventType.DELTA,
        MessageEventType.MESSAGE,
        MessageEventType.COMPLETED,
    ]
    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert [row.kind for row in rows] == ["message", "message", "inbound"]
    assert rows[0].content == [{"type": "text", "text": "完整消息"}]
    assert rows[1].content == [{"type": "text", "text": "先说的"}]


async def test_flush_persists_accumulated_delta_and_resets(store: Store) -> None:
    backend = FakeBackend(
        static_handler(
            text_frame(1, "delta", "第一段"),
            frame(2, "flush"),
            text_frame(3, "delta", "第二段"),
            frame(4, "completed"),
        )
    )
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [
        MessageEventType.DELTA,
        MessageEventType.FLUSH,
        MessageEventType.DELTA,
        MessageEventType.COMPLETED,
    ]
    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert [row.kind for row in rows] == ["message", "message", "inbound"]
    assert rows[0].content == [{"type": "text", "text": "第二段"}]
    assert rows[1].content == [{"type": "text", "text": "第一段"}]


async def test_sse_parser_ignores_comments_and_joins_multiline_data(store: Store) -> None:
    delta = text_frame(1, "delta", "多行")
    completed = frame(2, "completed")
    body = ": heartbeat\n\n"
    body += "event: message\n"
    for line in json.dumps(delta, ensure_ascii=False, indent=2).splitlines():
        body += f"data: {line}\n"
    body += "\n"
    body += f"id: 2\nretry: 100\ndata: {json.dumps(completed)}\n\n"
    backend = FakeBackend(lambda request: raw_sse_response(body))
    processor = make_processor(store, backend)

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.DELTA, MessageEventType.COMPLETED]
    assert events[0].content[0].text == "多行"


async def test_cancellation_propagates(store: Store) -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(10)
        return sse_response(frame(1, "completed"))

    processor = make_processor(store, FakeBackend(handler), spec=default_spec(timeout_ms=5000))
    message = make_message()

    async def consume() -> None:
        async for _event in processor(message):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processor.active_turns == 0


async def test_early_close_releases_in_flight_turn(store: Store) -> None:
    backend = FakeBackend(
        static_handler(text_frame(1, "delta", "一"), frame(2, "completed"))
    )
    clients: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=backend.transport)
        clients.append(client)
        return client

    processor = TurnProcessor(store, default_spec(timeout_ms=5000), http_client_factory=factory)
    stream = processor(make_message())

    first = await asyncio.wait_for(stream.__anext__(), timeout=2)
    assert first.type is MessageEventType.DELTA
    assert processor.active_turns == 1

    # The upstream closes the stream when it stops consuming (HANDOVER §11.2 #3):
    # the HTTP client must be released and the counter restored, without hanging.
    await asyncio.wait_for(stream.aclose(), timeout=2)
    assert processor.active_turns == 0
    assert clients[0].is_closed
    # The EOF/completed handling never ran, so no outbound row is (mis)recorded.
    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert [row.kind for row in rows] == ["inbound"]


async def test_clock_is_injectable(store: Store) -> None:
    backend = FakeBackend(
        static_handler(text_frame(1, "delta", "答案"), frame(2, "completed"))
    )
    processor = make_processor(store, backend, clock=lambda: 5555.0)

    await collect(processor, make_message())

    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert rows[0].kind == "message"
    assert rows[0].ts == 5555.0
    assert rows[1].ts == 1000.0


async def test_non_json_metadata_objects_are_sanitized(store: Store) -> None:
    """Platform SDK objects in metadata must not break the wire body or SQLite.

    octop-gateway's WeCom adapter puts live ``_frame`` / ``_ws_client`` objects
    into ``InboundMessage.metadata`` (see upstream ``channels/wecom.py``);
    serialising them would turn a normal turn into ``backend_error``.
    """
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = make_processor(store, backend)

    marker = object()
    message = make_message(
        metadata={
            "message_id": "om_1",
            "_frame": marker,
            "_ws_client": marker,
            "nested": {"ok": 1, "bad": marker},
            "list": [1, marker, "x"],
        }
    )

    events = await collect(processor, message)

    assert event_types(events) == [MessageEventType.COMPLETED]
    body = json.loads(backend.calls[0].content)
    expected = {"message_id": "om_1", "nested": {"ok": 1}, "list": [1, "x"]}
    assert body["metadata"] == expected
    rows = store.query_history(channel_id="feishu:acme:sales", subject_id="ou_1")
    assert rows[0].kind == "inbound"
    assert rows[0].metadata == expected


async def test_empty_backend_token_omits_authorization_header(store: Store) -> None:
    """An unset ``IMGW_BACKEND_TOKEN`` must not produce an illegal ``Bearer `` header."""
    backend = FakeBackend(static_handler(frame(1, "completed")))
    processor = make_processor(
        store,
        backend,
        spec=BackendSpec(base_url=BASE_URL, token="", timeout_ms=3000),
    )

    events = await collect(processor, make_message())

    assert event_types(events) == [MessageEventType.COMPLETED]
    assert "authorization" not in backend.calls[0].headers
