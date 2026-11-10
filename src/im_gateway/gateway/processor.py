"""Model-A turn processor: gateway -> backend over SSE (HANDOVER §12).

``octop-gateway`` calls ``processor(message)`` and immediately ``async for``-s the
result (HANDOVER §11.2 #1), so :class:`TurnProcessor` is an **async generator
function**, not a coroutine returning an iterator.

One turn is::

    InboundMessage
      -> claim idempotency key              (inbound_dedup, §13.2)
      -> persist inbound + upsert subject   (§13.9 / §11.2 #11)
      -> POST TurnFrame(kind=start) to the single backend's /v1/turns
      -> translate the SSE frame stream into MessageEvent objects
      -> exactly one terminal event (completed / error)

The backend is **one URL**, resolved from ``--backend-url`` >
``IMGW_BACKEND_URL`` > ``http://127.0.0.1:8080`` (GitHub issue #3). There is no
health check, probe, registration or per-tenant routing. Every request carries
reverse-proxy distribution headers (``X-Imgw-Channel-Id`` / ``-Name`` /
``X-Imgw-Kind``) so a backend can tell channels apart.

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
from im_gateway.db.models import ChannelRecord, MessageRecord, SubjectRecord
from im_gateway.db.store import Store
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.idempotency import build_idempotency_key, extract_platform_message_id

__all__ = [
    "DEFAULT_BACKEND_TIMEOUT_MS",
    "DEFAULT_BACKEND_URL",
    "DEFAULT_ERROR_TEXTS",
    "BackendSpec",
    "TurnProcessor",
    "build_processor",
    "resolve_backend_spec",
    "resolve_backend_url",
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

#: Default backend used when neither ``--backend-url`` nor ``IMGW_BACKEND_URL``
#: is set. There is no health check or registration: the URL is trusted.
DEFAULT_BACKEND_URL: Final[str] = "http://127.0.0.1:8080"

#: Default turn deadline (``IMGW_BACKEND_TIMEOUT_MS`` overrides it).
DEFAULT_BACKEND_TIMEOUT_MS: Final[int] = 120_000

#: Metadata keys that stay durable even if upstream adds them to
#: ``EPHEMERAL_PUSH_META`` (the iLink context token has a durable copy by design,
#: HANDOVER §11.3).
_DURABLE_META_KEYS: Final[frozenset[str]] = frozenset(
    {"weixin_context_token", "weixin_context_token_ms"}
)

#: Distribution headers let one backend tell channels apart (reverse-proxy style).
_HEADER_CHANNEL_ID: Final[str] = "X-Imgw-Channel-Id"
_HEADER_CHANNEL_NAME: Final[str] = "X-Imgw-Channel-Name"
_HEADER_KIND: Final[str] = "X-Imgw-Kind"


class _ProtocolError(Exception):
    """A backend frame stream violated the wire contract; message is user-facing."""


# ---------------------------------------------------------------------------
# Backend configuration: one URL, one token, one timeout (HANDOVER §16)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Everything the processor needs to call the one agent backend."""

    base_url: str
    token: str = ""
    timeout_ms: int = DEFAULT_BACKEND_TIMEOUT_MS


def resolve_backend_spec(
    *,
    backend_url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> BackendSpec:
    """Resolve the single backend from the CLI flag, the environment and defaults.

    Priority (GitHub issue #3): ``backend_url`` (``imgw run --backend-url``) >
    ``IMGW_BACKEND_URL`` > :data:`DEFAULT_BACKEND_URL`.  ``IMGW_BACKEND_TOKEN``
    is optional: when unset no ``Authorization`` header is sent.
    ``IMGW_BACKEND_TIMEOUT_MS`` is optional and defaults to
    :data:`DEFAULT_BACKEND_TIMEOUT_MS`; malformed / non-positive values fall
    back to that default rather than failing the daemon.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    raw_url = backend_url if backend_url is not None else environ.get("IMGW_BACKEND_URL", "")
    url = (raw_url or "").strip() or DEFAULT_BACKEND_URL
    token = environ.get("IMGW_BACKEND_TOKEN", "").strip()
    return BackendSpec(
        base_url=url.rstrip("/"),
        token=token,
        timeout_ms=_env_positive_int(environ.get("IMGW_BACKEND_TIMEOUT_MS", ""), DEFAULT_BACKEND_TIMEOUT_MS),
    )


def resolve_backend_url(
    channel_config: Mapping[str, Any] | None,
    global_url: str,
) -> str:
    """Return the effective backend URL for one channel.

    Future seam only: a channel's plain config dict may carry ``backend_url`` to
    override the gateway-wide backend (no CLI flag / help exposure yet).  The
    channel config is a plain JSON object, so this adds no table, column or
    wire-contract change; absent or blank values fall back to *global_url*.
    """
    override = (channel_config or {}).get("backend_url")
    if isinstance(override, str) and override.strip():
        return override.strip().rstrip("/")
    return global_url.rstrip("/")


def _env_positive_int(raw: str, default: int) -> int:
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return default
    return value if value > 0 else default


# ---------------------------------------------------------------------------
# Small module-level helpers (no state: easy to reason about / test)
# ---------------------------------------------------------------------------


def _new_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


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
        backend: BackendSpec | None = None,
        *,
        events: EventBus | None = None,
        capabilities_for_kind: Callable[[str], ChannelCapabilities] = capabilities_for,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.time,
        error_texts: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._backend = backend if backend is not None else resolve_backend_spec()
        self._events = events
        self._capabilities_for_kind = capabilities_for_kind
        self._http_client_factory = http_client_factory
        self._clock = clock
        merged = dict(DEFAULT_ERROR_TEXTS)
        if error_texts:
            merged.update(error_texts)
        self._error_texts = merged
        #: Turns currently being processed; the runtime uses it for graceful
        #: shutdown (stop accepting -> wait until this drops to 0).  A plain int
        #: is enough: the daemon has exactly one event loop.
        self._active = 0

    @property
    def backend(self) -> BackendSpec:
        """The single backend this processor posts to (read-only)."""
        return self._backend

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

            spec = self._backend
            record = self._store.get_channel(message.channel_id)
            backend_url = resolve_backend_url(
                record.config if record is not None else None, spec.base_url
            )
            deadline_ms = spec.timeout_ms
            start = start_from_inbound(
                message,
                session_key=subject.subject_id,
                idempotency_key=idempotency_key,
                deadline_ms=deadline_ms,
                capabilities=self._capabilities_for_kind(message.channel_type),
                turn_id=turn_id,
            )
            async with aclosing(
                self._stream_turn(
                    message,
                    subject,
                    backend_url,
                    self._request_headers(message, record),
                    start,
                    turn_id,
                )
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

    def _request_headers(
        self, message: InboundMessage, record: ChannelRecord | None
    ) -> dict[str, str]:
        """Build the POST headers, including reverse-proxy distribution headers.

        The channel id / kind come from the channel record when it exists (the
        record is the source of truth); they fall back to the inbound message so
        the processor still works when it is used without a persisted channel.
        An empty channel name omits ``X-Imgw-Channel-Name`` entirely.
        """
        channel_id = record.channel_id if record is not None else message.channel_id
        kind = record.kind if record is not None else (message.channel_type or "unknown")
        headers: dict[str, str] = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            _HEADER_CHANNEL_ID: channel_id,
            _HEADER_KIND: kind,
        }
        if record is not None and record.name:
            headers[_HEADER_CHANNEL_NAME] = record.name
        if self._backend.token:
            # An empty token must omit the header: httpx rejects a literal
            # "Bearer " as an illegal header value.
            headers["Authorization"] = f"Bearer {self._backend.token}"
        return headers

    async def _stream_turn(
        self,
        message: InboundMessage,
        subject: ChannelSubject,
        backend_url: str,
        request_headers: dict[str, str],
        start: TurnFrame,
        turn_id: str,
    ) -> AsyncGenerator[MessageEvent, None]:
        """POST the start frame and translate the SSE response into events."""
        client_factory: Callable[[], httpx.AsyncClient] = (
            self._http_client_factory or _new_async_client
        )
        #: Accumulated DELTA text — persisted as one message on COMPLETED/FLUSH,
        #: mirroring the channel's own accumulation (HANDOVER §13.9: no deltas).
        delta_buffer: list[str] = []

        async with client_factory() as client:
            async with asyncio.timeout(self._backend.timeout_ms / 1000):
                async with client.stream(
                    "POST",
                    f"{backend_url}{_TURNS_PATH}",
                    headers=request_headers,
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
    backend: BackendSpec | None = None,
    events: EventBus | None = None,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> TurnProcessor:
    """Build the production processor from SQLite settings (HANDOVER §13.2).

    ``settings.error_texts`` is merged over :data:`DEFAULT_ERROR_TEXTS` (keys are
    fixed).  The backend URL / token / timeout come from the CLI flag and
    environment (:func:`resolve_backend_spec`), never from the database.
    """
    error_texts = dict(DEFAULT_ERROR_TEXTS)
    stored_texts = store.get_setting("error_texts", {})
    if isinstance(stored_texts, dict):
        error_texts.update({str(key): str(value) for key, value in stored_texts.items()})
    return TurnProcessor(
        store,
        backend,
        events=events,
        http_client_factory=http_client_factory,
        error_texts=error_texts,
    )
