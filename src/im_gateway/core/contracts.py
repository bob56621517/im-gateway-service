"""Wire contract between the gateway and out-of-process agent backends.

Design (HANDOVER §12): **one envelope covers both directions**.

- The request body of ``POST {backend}/v1/turns`` is a :class:`TurnFrame` with
  ``kind=start`` — the first frame of the turn's event stream.
- The response body is a sequence of :class:`TurnFrame` objects of the same
  shape, framed as SSE ``data:`` lines.

The models here are deliberately **independent of octop-gateway's internal
enums**: the wire protocol must stay stable even when the upstream library
changes. Conversion helpers at the bottom do the mapping; upstream imports are
lazy so the CLI and config tooling can use this module without importing any
platform SDK.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octop_gateway.models import InboundMessage, MessageEvent

__all__ = [
    "TERMINAL_KINDS",
    "AudioContent",
    "ChannelCapabilities",
    "ChannelRef",
    "ContentPart",
    "ContentType",
    "FileContent",
    "FrameKind",
    "GroupContext",
    "GroupContextMessage",
    "ImageContent",
    "NativeOp",
    "SubjectRef",
    "TextContent",
    "TurnFrame",
    "VideoContent",
    "frame_to_message_event",
    "jsonable_metadata",
    "start_from_inbound",
]


# ---------------------------------------------------------------------------
# Content parts (mirrors octop-gateway ``models.py`` field names)
# ---------------------------------------------------------------------------


class ContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"


class TextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    type: Literal["image"] = "image"
    url: str = ""
    alt_text: str = ""
    width: int | None = None
    height: int | None = None
    size: int | None = None
    mime_type: str | None = None
    local_path: str | None = None
    data: str | None = None


class VideoContent(BaseModel):
    type: Literal["video"] = "video"
    url: str = ""
    duration: int | None = None
    thumbnail_url: str | None = None
    size: int | None = None
    mime_type: str | None = None
    local_path: str | None = None
    data: str | None = None


class AudioContent(BaseModel):
    type: Literal["audio"] = "audio"
    url: str = ""
    duration: int | None = None
    size: int | None = None
    mime_type: str | None = None
    local_path: str | None = None
    data: str | None = None


class FileContent(BaseModel):
    type: Literal["file"] = "file"
    url: str = ""
    filename: str = ""
    size: int | None = None
    mime_type: str | None = None
    local_path: str | None = None
    data: str | None = None


ContentPart = Annotated[
    TextContent | ImageContent | VideoContent | AudioContent | FileContent,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Group context (passive chatter attached to a group message)
# ---------------------------------------------------------------------------


class GroupContextMessage(BaseModel):
    message_id: str = ""
    sender_id: str
    sender_name: str = ""
    text: str
    content: list[ContentPart] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)


class GroupContext(BaseModel):
    conversation_id: str
    visibility: str
    activation: str
    messages: list[GroupContextMessage] = Field(default_factory=list)
    capability_degraded: bool = False


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class FrameKind(StrEnum):
    """The 11 frame kinds of the wire protocol (HANDOVER §12.3)."""

    START = "start"  # gateway -> backend; also the request body
    MESSAGE = "message"  # backend -> gateway
    DELTA = "delta"
    THINKING = "thinking"
    THINKING_DELTA = "thinking_delta"
    FLUSH = "flush"
    TYPING = "typing"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    NATIVE = "native"
    ERROR = "error"  # terminal
    COMPLETED = "completed"  # terminal


TERMINAL_KINDS: frozenset[FrameKind] = frozenset({FrameKind.ERROR, FrameKind.COMPLETED})


class ChannelRef(BaseModel):
    channel_id: str
    kind: str
    tenant_id: str | None = None


class SubjectRef(BaseModel):
    subject_id: str
    chat_type: str = ""
    display_name: str = ""


class ChannelCapabilities(BaseModel):
    """What the target channel can do right now (HANDOVER §12.7).

    Sent in ``start.capabilities`` so the backend can pick a send strategy
    without probing the gateway first. Values are configured per channel kind
    in :mod:`im_gateway.core.capabilities`.
    """

    streaming: Literal["native", "buffered"] = "buffered"
    markdown: Literal["native", "converted", "plain"] = "plain"
    media: dict[str, bool] = Field(default_factory=dict)
    max_text_len: int = 4000
    native_ops: list[str] = Field(default_factory=list)


class NativeOp(BaseModel):
    """Opaque platform-specific operation (HANDOVER §12.7).

    The gateway validates ``op`` against ``start.capabilities.native_ops`` and
    hands ``payload`` to the channel subclass unchanged. It never interprets
    the payload.
    """

    op: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TurnFrame(BaseModel):
    """The single envelope used for both request and response (HANDOVER §12.2)."""

    turn_id: str
    seq: int = 0
    kind: FrameKind
    timestamp: float = Field(default_factory=time.time)

    content: list[ContentPart] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    native: NativeOp | None = None

    # ---- start only (gateway -> backend) ----
    channel: ChannelRef | None = None
    subject: SubjectRef | None = None
    session_key: str | None = None
    idempotency_key: str | None = None
    deadline_ms: int | None = None
    capabilities: ChannelCapabilities | None = None
    group_context: GroupContext | None = None

    @model_validator(mode="after")
    def _validate_per_kind(self) -> TurnFrame:
        """Enforce the per-kind required-field table (HANDOVER §12.3)."""
        kind = self.kind

        def require(condition: bool, message: str) -> None:
            if not condition:
                raise ValueError(f"{kind.value} frame: {message}")

        if kind is FrameKind.START:
            require(self.channel is not None, "channel is required")
            require(self.subject is not None, "subject is required")
            require(bool(self.session_key), "session_key is required")
            require(bool(self.idempotency_key), "idempotency_key is required")
            require(self.deadline_ms is not None and self.deadline_ms > 0, "deadline_ms must be > 0")
            require(self.seq == 0, "seq must be 0")
        elif kind in (FrameKind.DELTA, FrameKind.THINKING, FrameKind.THINKING_DELTA):
            require(bool(self.content), "content is required")
            require(any(isinstance(part, TextContent) for part in self.content), "needs a text part")
        elif kind is FrameKind.MESSAGE:
            require(bool(self.content), "content is required")
        elif kind in (FrameKind.TOOL_START, FrameKind.TOOL_END):
            require(bool(self.metadata.get("tool_name")), "metadata.tool_name is required")
        elif kind is FrameKind.NATIVE:
            require(self.native is not None, "native is required")
            require(bool(self.native and self.native.op), "native.op is required")
        elif kind is FrameKind.ERROR:
            require(bool(self.error), "error text is required")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS

    @property
    def text(self) -> str:
        """Concatenated text of all text parts (convenience, not on the wire)."""
        return "\n".join(part.text for part in self.content if isinstance(part, TextContent) and part.text)

    def to_message_event(self) -> MessageEvent:
        """Convert a backend-produced frame to an octop-gateway ``MessageEvent``.

        ``native`` frames have no upstream equivalent and must be handled by the
        processor directly; calling this on one raises ``ValueError``.
        """
        from octop_gateway.models import MessageEvent, MessageEventType

        if self.kind is FrameKind.NATIVE:
            raise ValueError("native frames are not convertible to MessageEvent")
        return MessageEvent(
            type=MessageEventType(self.kind.value),
            content=[content_to_upstream(part) for part in self.content],
            metadata=dict(self.metadata),
            error=self.error,
        )


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


CONTENT_MODELS: dict[str, type[BaseModel]] = {
    "text": TextContent,
    "image": ImageContent,
    "video": VideoContent,
    "audio": AudioContent,
    "file": FileContent,
}


def _content_from_upstream(part: Any) -> ContentPart:
    """Map an octop-gateway content part into our wire model."""
    data = part.model_dump()
    return cast(ContentPart, CONTENT_MODELS[data["type"]].model_validate(data))


def content_to_upstream(part: ContentPart) -> Any:
    """Map our wire content part into the octop-gateway model (lazy import)."""
    from octop_gateway import models as upstream

    mapping: dict[type[BaseModel], type[Any]] = {
        TextContent: upstream.TextContent,
        ImageContent: upstream.ImageContent,
        VideoContent: upstream.VideoContent,
        AudioContent: upstream.AudioContent,
        FileContent: upstream.FileContent,
    }
    target = mapping[type(part)]
    return target.model_validate(part.model_dump())


def frame_to_message_event(frame: TurnFrame) -> MessageEvent:
    return frame.to_message_event()


_DROP = object()


def _jsonable(value: Any) -> Any:
    """Recursively keep JSON-representable values, drop everything else."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, dict):
        cleaned = {str(key): _jsonable(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item is not _DROP}
    if isinstance(value, list | tuple):
        items = [_jsonable(item) for item in value]
        return [item for item in items if item is not _DROP]
    return _DROP


def jsonable_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy *metadata*, dropping values that cannot be represented as JSON.

    Platform adapters put live SDK objects into ``InboundMessage.metadata``
    (e.g. WeCom's ``_frame`` / ``_ws_client``, see octop-gateway
    ``channels/wecom.py``). Passing those into the wire body or SQLite would
    fail serialisation and turn a normal message into an internal error, so the
    gateway sanitises them at the boundary (HANDOVER §11.2 #12).
    """
    if not metadata:
        return {}
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        cleaned = _jsonable(value)
        if cleaned is not _DROP:
            result[str(key)] = cleaned
    return result


#: Channel kinds whose transport only carries 1:1 chats.  Upstream leaves
#: ``ChannelSubject.chat_type`` empty for these (e.g. the WeChat iLink bot), so the
#: wire contract fills in the semantic default instead of sending an empty field.
_DIRECT_ONLY_KINDS: Final[frozenset[str]] = frozenset({"weixin"})


def _subject_chat_type(channel_type: str | None, chat_type: str | None) -> str:
    normalized = (chat_type or "").strip()
    if normalized:
        return normalized
    if (channel_type or "") in _DIRECT_ONLY_KINDS:
        return "direct"
    return ""


def start_from_inbound(
    message: InboundMessage,
    *,
    session_key: str,
    idempotency_key: str,
    deadline_ms: int,
    capabilities: ChannelCapabilities | None = None,
    turn_id: str,
) -> TurnFrame:
    """Build the ``start`` frame (request body) from an upstream InboundMessage."""
    from octop_gateway.models import GroupContext as UpstreamGroupContext

    subject = message.channel_subject
    if subject is None:
        raise ValueError("inbound message has no channel_subject")

    group_context = None
    raw = message.group_context
    if isinstance(raw, UpstreamGroupContext):
        group_context = GroupContext.model_validate(raw.model_dump())

    return TurnFrame(
        turn_id=turn_id,
        seq=0,
        kind=FrameKind.START,
        channel=ChannelRef(
            channel_id=message.channel_id,
            kind=message.channel_type or "unknown",
            tenant_id=message.tenant_id,
        ),
        subject=SubjectRef(
            subject_id=subject.subject_id,
            chat_type=_subject_chat_type(message.channel_type, subject.chat_type),
            display_name=subject.display_name or "",
        ),
        session_key=session_key,
        idempotency_key=idempotency_key,
        deadline_ms=deadline_ms,
        content=[_content_from_upstream(part) for part in message.content],
        metadata=jsonable_metadata(message.metadata),
        timestamp=message.timestamp,
        capabilities=capabilities,
        group_context=group_context,
    )
