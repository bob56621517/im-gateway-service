"""Model-A turn processor: gateway -> backend over SSE (HANDOVER §12).

``octop-gateway`` calls ``processor(message)`` and immediately ``async for``-s the
result (HANDOVER §11.2 #1), so :class:`TurnProcessor` is an **async generator
function**, not a coroutine returning an iterator.

One turn is::

    InboundMessage
      -> claim idempotency key              (inbound_dedup, §13.2)
      -> persist inbound + upsert subject   (§13.9 / §11.2 #11)
      -> POST TurnFrame(kind=start) to the tenant backend's /v1/turns
      -> translate the SSE frame stream into MessageEvent objects
      -> exactly one terminal event (completed / error)

Error policy (HANDOVER §11.2 #2): the upstream swallows processor exceptions and
sends a fixed English string, so *every* failure is converted into an in-band
``error_event`` here.  ``asyncio.CancelledError`` is the single exception that is
allowed to propagate so the upstream can cancel the turn correctly.

A backend must terminate its frame stream with ``completed`` or ``error``: an
SSE EOF without a terminal frame becomes an ``error`` event, because the channel
only flushes accumulated deltas on a terminal event (HANDOVER §11.2 #3).

``native`` frames are a protocol error in v1: no channel advertises native ops
yet (``core/capabilities.py``), so a backend that sends one is programmed against
a capability it was never told it had.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Final

import httpx
from octop_gateway.models import ChannelSubject, InboundMessage, MessageEvent
from octop_gateway.push_routing import EPHEMERAL_PUSH_META
from pydantic import ValidationError

from im_gateway.core.capabilities import capabilities_for
from im_gateway.core.contracts import (
    ChannelCapabilities,
    FrameKind,
    TurnFrame,
    jsonable_metadata,
    start_from_inbound,
)
from im_gateway.db.models import MessageRecord, SubjectRecord
from im_gateway.db.store import Store
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.idempotency import build_idempotency_key, extract_platform_message_id

__all__ = [
    "DEFAULT_ERROR_TEXTS",
    "BackendRouter",
    "BackendSpec",
    "TurnProcessor",
    "build_processor",
]

logger = logging.getLogger(__name__)

#: User-facing Chinese error texts.  Keys are fixed; ``settings.error_texts``
#: may override values (never the key set) at runtime.
DEFAULT_ERROR_TEXTS: dict[str, str] = {
    "backend_down": "后端暂不可用，请稍后重试",
    "backend_timeout": "处理超时，请稍后重试",
    "backend_http": "后端返回异常状态",
    "backend_incomplete": "后端未返回完成帧",
    "backend_error": "后端处理失败",
}

_MISSING_SUBJECT: Final[str] = "消息缺少会话目标"
_INVALID_FRAME: Final[str] = "后端返回了无效帧"
_BAD_SEQUENCE: Final[str] = "后端帧序号不连续"
_UNSUPPORTED_NATIVE: Final[str] = "当前网关不支持原生能力"
_UNEXPECTED_START: Final[str] = "后端返回了不应出现的 start 帧"

_TURNS_PATH: Final[str] = "/v1/turns"

#: ``token_ref`` is either an env reference (``${VAR}`` / ``$VAR``) or a literal.
_ENV_REF_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$"
)

#: Metadata keys that stay durable even if upstream adds them to
#: ``EPHEMERAL_PUSH_META`` (the iLink context token has a durable copy by design,
#: HANDOVER §11.3).
_DURABLE_META_KEYS: Final[frozenset[str]] = frozenset(
    {"weixin_context_token", "weixin_context_token_ms"}
)

#: Fallback deadline used when neither the backend row nor settings define one.
_FALLBACK_DEADLINE_MS: Final[int] = 120_000


class _ProtocolError(Exception):
    """A backend frame stream violated the wire contract; message is user-facing."""


# ---------------------------------------------------------------------------
# Backend routing (tenant -> backend webhook, HANDOVER §13.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Everything the processor needs to call one backend."""

    tenant_id: str
    base_url: str
    token: str
    timeout_ms: int
    enabled: bool


class BackendRouter:
    """Resolve ``InboundMessage.tenant_id`` to the matching backend row.

    Routing is exact-match first, then the default backend (``tenant_id=""``).
    Secret material is stored as an environment reference in the DB
    (HANDOVER §13.5) and expanded in memory here; a missing variable expands to
    an empty token instead of failing the turn — the backend will answer 401 and
    the operator sees the configured-but-missing env var in the request.
    """

    def __init__(self, store: Store, *, env: Mapping[str, str] | None = None) -> None:
        self._store = store
        self._env: Mapping[str, str] = os.environ if env is None else env

    def resolve(self, tenant_id: str | None) -> BackendSpec | None:
        """Return the backend for *tenant_id*, falling back to the default one."""
        lookup = tenant_id or ""
        record = self._store.get_backend(lookup)
        if record is None and lookup:
            record = self._store.get_backend("")
        if record is None or not record.enabled:
            return None
        return BackendSpec(
            tenant_id=record.tenant_id,
            base_url=record.base_url.rstrip("/"),
            token=self._expand_token(record.token_ref),
            timeout_ms=record.timeout_ms,
            enabled=record.enabled,
        )

    def _expand_token(self, token_ref: str) -> str:
        match = _ENV_REF_PATTERN.match(token_ref.strip())
        if match is None:
            return token_ref
        name = match.group(1) or match.group(2)
        return self._env.get(name, "")


# ---------------------------------------------------------------------------
# Small module-level helpers (no state: easy to reason about / test)
# ---------------------------------------------------------------------------


def _new_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _coerce_positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _durable_subject_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only metadata that survives a proactive push (HANDOVER §11.2 #12)."""
    if not metadata:
        return {}
    clean = jsonable_metadata(metadata)
    return {
        key: value
        for key, value in clean.items()
        if key not in EPHEMERAL_PUSH_META or key in _DURABLE_META_KEYS
    }


def _json_content(parts: list[Any]) -> list[dict[str, Any]]:
    return [part.model_dump(mode="json") for part in parts]


# ---------------------------------------------------------------------------
# TurnProcessor
# ---------------------------------------------------------------------------


class TurnProcessor:
    """The ``MessageProcessor`` implementation plugged into ``ChannelManager``.

    ``store``, ``EventBus`` and the HTTP client factory are injected so tests run
    without a network or platform SDK; the production wiring lives in
    :func:`build_processor` / ``gateway.runtime``.
    """

    def __init__(
        self,
        store: Store,
        router: BackendRouter,
        *,
        events: EventBus | None = None,
        capabilities_for_kind: Callable[[str], ChannelCapabilities] = capabilities_for,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.time,
        error_texts: dict[str, str] | None = None,
        default_deadline_ms: int = _FALLBACK_DEADLINE_MS,
    ) -> None:
        self._store = store
        self._router = router
        self._events = events
        self._capabilities_for_kind = capabilities_for_kind
        self._http_client_factory = http_client_factory
        self._clock = clock
        merged = dict(DEFAULT_ERROR_TEXTS)
        if error_texts:
            merged.update(error_texts)
        self._error_texts = merged
        self._default_deadline_ms = default_deadline_ms
        #: Turns currently being processed; the runtime uses it for graceful
        #: shutdown (stop accepting -> wait until this drops to 0).  A plain int
        #: is enough: the daemon has exactly one event loop.
        self._active = 0

    @property
    def active_turns(self) -> int:
        """Number of turns currently in flight (graceful-shutdown drain)."""
        return self._active

    async def __call__(self, message: InboundMessage) -> AsyncIterator[MessageEvent]:
        """Process one inbound message (async generator, HANDOVER §11.2 #1)."""
        self._active += 1
        try:
            # ``aclosing`` makes early consumer stop (the upstream closes the
            # stream after an ERROR, HANDOVER §11.2 #3) release the HTTP client
            # deterministically instead of waiting for async-generator GC.
            async with aclosing(self._run_turn(message)) as turn:
                async for event in turn:
                    yield event
        finally:
            self._active -= 1

    # -- turn orchestration ----------------------------------------------------

    async def _run_turn(self, message: InboundMessage) -> AsyncGenerator[MessageEvent, None]:
        """One full turn; never lets an exception escape except cancellation."""
        subject = message.channel_subject
        if subject is None:
            yield MessageEvent.error_event(_MISSING_SUBJECT)
            return

        turn_id: str | None = None
        try:
            idempotency_key = build_idempotency_key(message)
            # Duplicate platform redelivery: the first turn already answered it.
            if not self._store.claim_inbound(idempotency_key, message.channel_id):
                yield MessageEvent.completed()
                return

            turn_id = uuid.uuid4().hex
            self._record_inbound(message, subject, turn_id)
            self._upsert_subject(message, subject)
            self._publish(
                {
                    "type": "message.in",
                    "channel_id": message.channel_id,
                    "subject_id": subject.subject_id,
                    "turn_id": turn_id,
                    "ts": self._clock(),
                }
            )

            spec = self._router.resolve(message.tenant_id)
            if spec is None or not spec.enabled:
                yield self._record_outbound_error(
                    message, subject, turn_id, self._error_texts["backend_down"]
                )
                return

            deadline_ms = spec.timeout_ms or self._default_deadline_ms
            start = start_from_inbound(
                message,
                session_key=subject.subject_id,
                idempotency_key=idempotency_key,
                deadline_ms=deadline_ms,
                capabilities=self._capabilities_for_kind(message.channel_type),
                turn_id=turn_id,
            )
            async with aclosing(
                self._stream_turn(message, subject, spec, start, turn_id)
            ) as events:
                async for event in events:
                    yield event
        except asyncio.CancelledError:
            raise
        except _ProtocolError as exc:
            yield self._record_outbound_error(message, subject, turn_id, str(exc))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            yield self._record_outbound_error(
                message, subject, turn_id, self._error_texts["backend_down"]
            )
        except (TimeoutError, httpx.TimeoutException):
            yield self._record_outbound_error(
                message, subject, turn_id, self._error_texts["backend_timeout"]
            )
        except Exception:  # contract: report in-band, never raise
            logger.exception("turn failed: channel=%s", message.channel_id)
            yield self._record_outbound_error(
                message, subject, turn_id, self._error_texts["backend_error"]
            )

    # -- backend stream --------------------------------------------------------

    async def _stream_turn(
        self,
        message: InboundMessage,
        subject: ChannelSubject,
        spec: BackendSpec,
        start: TurnFrame,
        turn_id: str,
    ) -> AsyncGenerator[MessageEvent, None]:
        """POST the start frame and translate the SSE response into events."""
        deadline_ms = start.deadline_ms or self._default_deadline_ms
        client_factory: Callable[[], httpx.AsyncClient] = (
            self._http_client_factory or _new_async_client
        )
        #: Accumulated DELTA text — persisted as one message on COMPLETED/FLUSH,
        #: mirroring the channel's own accumulation (HANDOVER §13.9: no deltas).
        delta_buffer: list[str] = []

        async with client_factory() as client:
            async with asyncio.timeout(deadline_ms / 1000):
                async with client.stream(
                    "POST",
                    f"{spec.base_url}{_TURNS_PATH}",
                    headers={
                        "Authorization": f"Bearer {spec.token}",
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                    },
                    content=start.model_dump_json(),
                ) as response:
                    if response.status_code != 200:
                        text = f"{self._error_texts['backend_http']} ({response.status_code})"
                        yield self._record_outbound_error(message, subject, turn_id, text)
                        return

                    expected_seq: int | None = None
                    async for frame in self._iter_frames(response):
                        kind = frame.kind
                        # Illegal response kinds are reported before seq checking:
                        # a well-formed ``start`` frame is forced to seq=0 by the
                        # contract, which must not masquerade as a seq violation.
                        if kind is FrameKind.START:
                            yield self._record_outbound_error(
                                message, subject, turn_id, _UNEXPECTED_START
                            )
                            return
                        if kind is FrameKind.NATIVE:
                            yield self._record_outbound_error(
                                message, subject, turn_id, _UNSUPPORTED_NATIVE
                            )
                            return

                        if expected_seq is None:
                            if frame.seq < 1:
                                yield self._record_outbound_error(
                                    message, subject, turn_id, _BAD_SEQUENCE
                                )
                                return
                            expected_seq = frame.seq + 1
                        elif frame.seq != expected_seq:
                            yield self._record_outbound_error(
                                message, subject, turn_id, _BAD_SEQUENCE
                            )
                            return
                        else:
                            expected_seq += 1

                        if kind is FrameKind.ERROR:
                            text = frame.error or self._error_texts["backend_error"]
                            yield self._record_outbound_error(message, subject, turn_id, text)
                            return
                        if kind is FrameKind.COMPLETED:
                            self._flush_delta_text(message, subject, turn_id, delta_buffer)
                            yield MessageEvent.completed()
                            return
                        if kind is FrameKind.MESSAGE:
                            # The channel flushes pending deltas before a complete
                            # message, so persist them in the same order.
                            self._flush_delta_text(message, subject, turn_id, delta_buffer)
                            self._record_outbound_message(
                                message,
                                subject,
                                turn_id,
                                _json_content(list(frame.content)),
                            )
                            yield frame.to_message_event()
                            continue
                        if kind is FrameKind.FLUSH:
                            self._flush_delta_text(message, subject, turn_id, delta_buffer)
                            yield frame.to_message_event()
                            continue
                        if kind is FrameKind.DELTA:
                            text = frame.text
                            if text:
                                delta_buffer.append(text)
                            yield frame.to_message_event()
                            continue
                        yield frame.to_message_event()

                    # EOF without a terminal frame: the channel would discard the
                    # accumulated deltas, so synthesise the terminal error here.
                    yield self._record_outbound_error(
                        message, subject, turn_id, self._error_texts["backend_incomplete"]
                    )

    async def _iter_frames(self, response: httpx.Response) -> AsyncIterator[TurnFrame]:
        """Parse an SSE body into validated :class:`TurnFrame` objects.

        Implements the small subset of the SSE grammar the contract uses:
        blank line = dispatch, ``:`` comment/heartbeat = ignore, ``event`` /
        ``id`` / ``retry`` fields = ignore, multiple ``data:`` lines of one frame
        are joined with ``\\n``.
        """
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if data_lines:
                    yield self._parse_frame("\n".join(data_lines))
                    data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if field in ("event", "id", "retry"):
                continue
            if field != "data":
                continue
            data_lines.append(value[1:] if value.startswith(" ") else value)
        if data_lines:
            yield self._parse_frame("\n".join(data_lines))

    @staticmethod
    def _parse_frame(payload: str) -> TurnFrame:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise _ProtocolError(_INVALID_FRAME) from exc
        try:
            return TurnFrame.model_validate(raw)
        except ValidationError as exc:
            raise _ProtocolError(_INVALID_FRAME) from exc

    # -- persistence (best-effort for outbound, HANDOVER §13.9) ----------------

    def _record_inbound(
        self, message: InboundMessage, subject: ChannelSubject, turn_id: str
    ) -> None:
        """Persist the inbound message *before* the backend is called (§13.9)."""
        self._store.add_message(
            MessageRecord(
                channel_id=message.channel_id,
                subject_id=subject.subject_id,
                session_key=subject.subject_id,
                direction="in",
                kind="inbound",
                content=_json_content(list(message.content)),
                metadata=jsonable_metadata(message.metadata),
                platform_msg_id=extract_platform_message_id(message),
                ts=message.timestamp,
                error=None,
                turn_id=turn_id,
            )
        )

    def _upsert_subject(self, message: InboundMessage, subject: ChannelSubject) -> None:
        """Refresh the durable address book row on *every* inbound message.

        Upstream keeps the subject registry in memory only (HANDOVER §11.2 #11),
        so a daemon restart relies on this table for proactive push.
        """
        existing = self._store.get_subject(message.channel_id, subject.subject_id)
        self._store.upsert_subject(
            SubjectRecord(
                channel_id=message.channel_id,
                subject_id=subject.subject_id,
                chat_type=subject.chat_type or "",
                display_name=subject.display_name or "",
                metadata=_durable_subject_metadata(message.metadata),
                first_seen=existing.first_seen if existing is not None else message.timestamp,
                last_seen=message.timestamp,
            )
        )

    def _record_outbound_message(
        self,
        message: InboundMessage,
        subject: ChannelSubject,
        turn_id: str,
        content: list[dict[str, Any]],
    ) -> None:
        """Best-effort outbound message row; never breaks the turn."""
        try:
            self._store.add_message(
                MessageRecord(
                    channel_id=message.channel_id,
                    subject_id=subject.subject_id,
                    session_key=subject.subject_id,
                    direction="out",
                    kind="message",
                    content=content,
                    metadata={"turn_id": turn_id},
                    platform_msg_id=None,
                    ts=self._clock(),
                    error=None,
                    turn_id=turn_id,
                )
            )
        except Exception:  # history is diagnostics, not state
            logger.exception("failed to record outbound message: channel=%s", message.channel_id)
            return
        self._publish_outbound(message, subject, turn_id, "message")

    def _record_outbound_error(
        self,
        message: InboundMessage,
        subject: ChannelSubject,
        turn_id: str | None,
        text: str,
    ) -> MessageEvent:
        """Record an outbound error row and return the matching error event."""
        event = MessageEvent.error_event(text)
        try:
            self._store.add_message(
                MessageRecord(
                    channel_id=message.channel_id,
                    subject_id=subject.subject_id,
                    session_key=subject.subject_id,
                    direction="out",
                    kind="error",
                    content=[{"type": "text", "text": text}],
                    metadata={"turn_id": turn_id},
                    platform_msg_id=None,
                    ts=self._clock(),
                    error=text,
                    turn_id=turn_id,
                )
            )
        except Exception:  # history is diagnostics, not state
            logger.exception("failed to record outbound error: channel=%s", message.channel_id)
            return event
        self._publish_outbound(message, subject, turn_id, "error")
        return event

    def _flush_delta_text(
        self,
        message: InboundMessage,
        subject: ChannelSubject,
        turn_id: str,
        delta_buffer: list[str],
    ) -> None:
        """Persist accumulated DELTA text as one outbound message and reset."""
        if not delta_buffer:
            return
        text = "".join(delta_buffer)
        delta_buffer.clear()
        if text:
            self._record_outbound_message(
                message, subject, turn_id, [{"type": "text", "text": text}]
            )

    # -- events ----------------------------------------------------------------

    def _publish(self, event: dict[str, Any]) -> None:
        """Best-effort publish on the control-plane event bus."""
        if self._events is None:
            return
        try:
            self._events.publish(event)
        except Exception:  # observation stream, not a failure path
            logger.exception("failed to publish event %r", event.get("type"))

    def _publish_outbound(
        self,
        message: InboundMessage,
        subject: ChannelSubject,
        turn_id: str | None,
        kind: str,
    ) -> None:
        self._publish(
            {
                "type": "message.out",
                "channel_id": message.channel_id,
                "subject_id": subject.subject_id,
                "turn_id": turn_id,
                "kind": kind,
                "ts": self._clock(),
            }
        )


# ---------------------------------------------------------------------------
# Production wiring
# ---------------------------------------------------------------------------


def build_processor(
    store: Store,
    *,
    events: EventBus | None = None,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> TurnProcessor:
    """Build the production processor from SQLite settings (HANDOVER §13.2).

    ``settings.error_texts`` is merged over :data:`DEFAULT_ERROR_TEXTS` (keys are
    fixed) and ``settings.turn_deadline_ms`` overrides the fallback deadline.
    """
    error_texts = dict(DEFAULT_ERROR_TEXTS)
    stored_texts = store.get_setting("error_texts", {})
    if isinstance(stored_texts, dict):
        error_texts.update({str(key): str(value) for key, value in stored_texts.items()})
    default_deadline_ms = _coerce_positive_int(
        store.get_setting("turn_deadline_ms", _FALLBACK_DEADLINE_MS),
        _FALLBACK_DEADLINE_MS,
    )
    return TurnProcessor(
        store,
        BackendRouter(store),
        events=events,
        http_client_factory=http_client_factory,
        error_texts=error_texts,
        default_deadline_ms=default_deadline_ms,
    )
