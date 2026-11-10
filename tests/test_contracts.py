"""Contract tests for the single-envelope wire protocol (HANDOVER §12)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from im_gateway.core.capabilities import capabilities_for
from im_gateway.core.contracts import (
    TERMINAL_KINDS,
    ChannelCapabilities,
    ChannelRef,
    FrameKind,
    GroupContext,
    GroupContextMessage,
    NativeOp,
    SubjectRef,
    TextContent,
    TurnFrame,
    content_to_upstream,
    start_from_inbound,
)


def make_start(**overrides: object) -> TurnFrame:
    values: dict[str, object] = {
        "turn_id": "01TESTTURN",
        "kind": FrameKind.START,
        "channel": ChannelRef(channel_id="feishu:acme:sales", kind="feishu", tenant_id="acme"),
        "subject": SubjectRef(subject_id="ou_1", chat_type="direct", display_name="张三"),
        "session_key": "ou_1",
        "idempotency_key": "feishu:om_1",
        "deadline_ms": 120_000,
        "content": [TextContent(text="hello")],
        "capabilities": capabilities_for("feishu"),
    }
    values.update(overrides)
    return TurnFrame.model_validate(values)


def test_start_frame_json_roundtrip() -> None:
    frame = make_start()
    raw = frame.model_dump_json()
    payload = json.loads(raw)
    assert payload["kind"] == "start"
    assert payload["seq"] == 0
    assert payload["channel"]["channel_id"] == "feishu:acme:sales"
    assert payload["capabilities"]["streaming"] == "buffered"
    again = TurnFrame.model_validate_json(raw)
    assert again == frame


@pytest.mark.parametrize(
    "missing",
    ["channel", "subject", "session_key", "idempotency_key", "deadline_ms"],
)
def test_start_requires_fields(missing: str) -> None:
    values = make_start().model_dump()
    values[missing] = None
    with pytest.raises(ValidationError):
        TurnFrame.model_validate(values)


def test_start_seq_must_be_zero() -> None:
    with pytest.raises(ValidationError):
        make_start(seq=1)


def test_start_deadline_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_start(deadline_ms=0)


@pytest.mark.parametrize("kind", [FrameKind.DELTA, FrameKind.THINKING, FrameKind.THINKING_DELTA])
def test_delta_needs_text(kind: FrameKind) -> None:
    with pytest.raises(ValidationError):
        TurnFrame(turn_id="t", seq=1, kind=kind, content=[])
    frame = TurnFrame(turn_id="t", seq=1, kind=kind, content=[TextContent(text="x")])
    assert frame.text == "x"


def test_message_needs_content() -> None:
    with pytest.raises(ValidationError):
        TurnFrame(turn_id="t", seq=1, kind=FrameKind.MESSAGE)


def test_tool_frames_need_tool_name() -> None:
    for kind in (FrameKind.TOOL_START, FrameKind.TOOL_END):
        with pytest.raises(ValidationError):
            TurnFrame(turn_id="t", seq=1, kind=kind)
        frame = TurnFrame(turn_id="t", seq=1, kind=kind, metadata={"tool_name": "search"})
        assert frame.metadata["tool_name"] == "search"


def test_error_needs_text() -> None:
    with pytest.raises(ValidationError):
        TurnFrame(turn_id="t", seq=1, kind=FrameKind.ERROR)
    frame = TurnFrame(turn_id="t", seq=1, kind=FrameKind.ERROR, error="后端暂不可用")
    assert frame.is_terminal


def test_native_needs_op() -> None:
    with pytest.raises(ValidationError):
        TurnFrame(turn_id="t", seq=1, kind=FrameKind.NATIVE)
    frame = TurnFrame(
        turn_id="t",
        seq=1,
        kind=FrameKind.NATIVE,
        native=NativeOp(op="feishu.card", payload={"schema": "2.0"}),
    )
    assert frame.native is not None
    assert frame.native.op == "feishu.card"


@pytest.mark.parametrize("kind", [FrameKind.TYPING, FrameKind.FLUSH, FrameKind.COMPLETED])
def test_bare_frames_ok(kind: FrameKind) -> None:
    frame = TurnFrame(turn_id="t", seq=1, kind=kind)
    assert frame.is_terminal is (kind is FrameKind.COMPLETED)


def test_terminal_kinds() -> None:
    assert {FrameKind.ERROR, FrameKind.COMPLETED} == TERMINAL_KINDS


def test_to_message_event_mapping() -> None:
    from octop_gateway.models import MessageEventType

    delta = TurnFrame(turn_id="t", seq=1, kind=FrameKind.DELTA, content=[TextContent(text="你好")])
    event = delta.to_message_event()
    assert event.type is MessageEventType.DELTA
    assert event.content[0].text == "你好"

    completed = TurnFrame(turn_id="t", seq=2, kind=FrameKind.COMPLETED)
    assert completed.to_message_event().type is MessageEventType.COMPLETED

    with pytest.raises(ValueError):
        TurnFrame(turn_id="t", seq=1, kind=FrameKind.NATIVE, native=NativeOp(op="x")).to_message_event()


def test_content_roundtrip_through_upstream() -> None:
    frame = TurnFrame(
        turn_id="t",
        seq=1,
        kind=FrameKind.MESSAGE,
        content=[TextContent(text="a")],
    )
    upstream = content_to_upstream(frame.content[0])
    assert upstream.text == "a"
    assert type(upstream).__name__ == "TextContent"


def test_start_from_inbound_maps_fields() -> None:
    from octop_gateway.models import (
        ChannelSubject,
        InboundMessage,
    )
    from octop_gateway.models import (
        GroupContext as UpstreamGroupContext,
    )
    from octop_gateway.models import (
        GroupContextMessage as UpstreamGroupContextMessage,
    )
    from octop_gateway.models import (
        TextContent as UpstreamText,
    )

    message = InboundMessage(
        channel_id="feishu:acme:sales",
        channel_type="feishu",
        tenant_id="acme",
        channel_subject=ChannelSubject(subject_id="ou_1", chat_type="group", display_name="群"),
        content=[UpstreamText(text="@bot hi")],
        metadata={"message_id": "om_1", "thread_id": "omt_1"},
        group_context=UpstreamGroupContext(
            conversation_id="oc_1",
            visibility="mention",
            activation="mention",
            messages=[
                UpstreamGroupContextMessage(
                    message_id="om_0", sender_id="ou_2", sender_name="李四", text="背景"
                )
            ],
        ),
    )
    frame = start_from_inbound(
        message,
        session_key="ou_1",
        idempotency_key="feishu:om_1",
        deadline_ms=30_000,
        capabilities=capabilities_for("feishu"),
        turn_id="01TEST",
    )
    assert frame.kind is FrameKind.START
    assert frame.channel is not None and frame.channel.tenant_id == "acme"
    assert frame.subject is not None and frame.subject.chat_type == "group"
    assert frame.metadata["message_id"] == "om_1"
    assert frame.group_context is not None
    assert frame.group_context.messages[0].sender_name == "李四"
    assert frame.capabilities is not None


@pytest.mark.parametrize(
    ("channel_type", "upstream_chat_type", "expected"),
    [
        ("weixin", "", "direct"),  # WeChat iLink bot leaves chat_type empty
        ("weixin", "group", "group"),  # explicit value always wins
        ("feishu", "", ""),  # other kinds stay empty: no guessing
    ],
)
def test_start_from_inbound_chat_type_defaults(
    channel_type: str, upstream_chat_type: str, expected: str
) -> None:
    from octop_gateway.models import ChannelSubject, InboundMessage
    from octop_gateway.models import TextContent as UpstreamText

    message = InboundMessage(
        channel_id=f"{channel_type}:acme:test",
        channel_type=channel_type,
        tenant_id=None,
        channel_subject=ChannelSubject(subject_id="u_1", chat_type=upstream_chat_type),
        content=[UpstreamText(text="hi")],
        metadata={},
    )
    frame = start_from_inbound(
        message,
        session_key="u_1",
        idempotency_key="k_1",
        deadline_ms=1_000,
        capabilities=capabilities_for(channel_type),
        turn_id="01TEST",
    )

    assert frame.subject is not None
    assert frame.subject.chat_type == expected


def test_capabilities_table_covers_all_upstream_kinds() -> None:
    from octop_gateway.channels import SUPPORTED_CHANNEL_KINDS

    from im_gateway.core.capabilities import CAPABILITIES

    # Guard against drift: every upstream channel kind must have a capability
    # entry so the start frame can always describe the target channel.
    assert set(SUPPORTED_CHANNEL_KINDS) <= set(CAPABILITIES)


def test_default_capabilities_are_conservative() -> None:
    caps = capabilities_for("unknown-platform")
    assert caps == ChannelCapabilities()
    assert caps.streaming == "buffered"
    assert caps.markdown == "plain"


def test_group_context_model_roundtrip() -> None:
    ctx = GroupContext(
        conversation_id="oc_1",
        visibility="mention",
        activation="mention",
        messages=[GroupContextMessage(sender_id="ou_2", text="背景")],
    )
    assert GroupContext.model_validate_json(ctx.model_dump_json()) == ctx

def test_jsonable_metadata_drops_objects_and_keeps_scalars() -> None:
    from im_gateway.core.contracts import jsonable_metadata

    marker = object()
    cleaned = jsonable_metadata(
        {
            "message_id": "om_1",
            "_frame": marker,
            "count": 3,
            "ratio": 0.5,
            "flag": True,
            "none": None,
            "nested": {"keep": "v", "drop": marker},
            "items": [1, marker, "x", {"deep": marker, "ok": 2}],
            "tuple": (1, marker),
        }
    )
    assert cleaned == {
        "message_id": "om_1",
        "count": 3,
        "ratio": 0.5,
        "flag": True,
        "none": None,
        "nested": {"keep": "v"},
        "items": [1, "x", {"ok": 2}],
        "tuple": [1],
    }


def test_start_frame_with_sdk_metadata_is_serializable() -> None:
    import json

    from octop_gateway.models import ChannelSubject, InboundMessage
    from octop_gateway.models import TextContent as UpstreamText

    from im_gateway.core.capabilities import capabilities_for
    from im_gateway.core.contracts import jsonable_metadata, start_from_inbound

    message = InboundMessage(
        channel_id="wecom:acme:cs",
        channel_type="wecom",
        tenant_id="acme",
        channel_subject=ChannelSubject(subject_id="user_1", chat_type="direct"),
        content=[UpstreamText(text="hi")],
        metadata={"msg_id": "m1", "_frame": object(), "_ws_client": object()},
    )
    frame = start_from_inbound(
        message,
        session_key="user_1",
        idempotency_key="wecom:acme:cs:m1",
        deadline_ms=1000,
        capabilities=capabilities_for("wecom"),
        turn_id="t1",
    )
    body = frame.model_dump_json()
    assert json.loads(body)["metadata"] == {"msg_id": "m1"}
    assert jsonable_metadata(message.metadata) == {"msg_id": "m1"}
