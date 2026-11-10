"""Gateway runtime: ``Store`` + octop-gateway ``ChannelManager`` wiring.

This module is the only place where control-plane state (SQLite) meets the
upstream channel runtime.  It enforces the host contract from HANDOVER §11.2:

* channel registration failures are isolated: startup records ``state="error"``
  instead of aborting the process (upstream ``manager.start()`` only logs them),
* there is no upstream ``update_config`` API, so changes are applied as
  ``remove_channel`` + ``add_channel`` (HANDOVER §13.4),
* per-channel health is tracked here because upstream exposes no uniform
  connection-state API (HANDOVER §11.2 #5),
* ``weixin`` config is built as a real upstream object because a dict cannot
  express ``accounts`` (HANDOVER §11.2 #14).

Every state transition is published on the process-local :class:`EventBus` so
``/v1/events`` observers (and the CLI in milestone 2) can follow it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from im_gateway.core import paths
from im_gateway.core.config import (
    MASKED_SECRET,
    ChannelConfigEntry,
    ConfigDocument,
)
from im_gateway.db.models import ChannelRecord, MessageRecord
from im_gateway.db.store import Store
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.factory import build_config, constraints_of
from im_gateway.gateway.processor import BackendSpec, build_processor, resolve_backend_spec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octop_gateway.channel import MessageProcessor

__all__ = ["GatewayRuntime"]

logger = logging.getLogger(__name__)

STATE_RUNNING: Final[str] = "running"
STATE_DISABLED: Final[str] = "disabled"
STATE_ERROR: Final[str] = "error"

_DEFAULT_WORKERS: Final[int] = 4
_DEFAULT_QUEUE_MAXSIZE: Final[int] = 1000
#: How long shutdown waits for in-flight turns before cancelling workers.
_DEFAULT_SHUTDOWN_GRACE_MS: Final[int] = 10_000
#: Audit rows written by the control plane through its own bearer token.
_ACTOR: Final[str] = "api-token"
#: Fields accepted by :meth:`GatewayRuntime.update_channel`.
_CHANNEL_UPDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"kind", "alias", "name", "enabled", "config", "constraints"}
)
#: Marker for "a masked value has no old value to fall back to".
_MISSING: Final[object] = object()


# ---------------------------------------------------------------------------
# Small helpers (module level: no state, easy to test)
# ---------------------------------------------------------------------------


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _resolve_masked(value: Any, existing: Any) -> Any:
    """Replace ``"***"`` leaves in *value* with the corresponding *existing* value.

    Returns :data:`_MISSING` for a masked leaf that has no old value; callers
    run :func:`_raise_if_missing` before touching the database.
    """
    if isinstance(value, str):
        return existing if value == MASKED_SECRET else value
    if isinstance(value, dict):
        base = existing if isinstance(existing, dict) else {}
        return {key: _resolve_masked(item, base.get(key, _MISSING)) for key, item in value.items()}
    if isinstance(value, list):
        if isinstance(existing, list) and len(existing) == len(value):
            return [_resolve_masked(item, existing[index]) for index, item in enumerate(value)]
        return [_resolve_masked(item, _MISSING) for item in value]
    return value


def _find_missing(value: Any) -> bool:
    if value is _MISSING:
        return True
    if isinstance(value, dict):
        return any(_find_missing(item) for item in value.values())
    if isinstance(value, list):
        return any(_find_missing(item) for item in value)
    return False


def _raise_if_missing(value: Any, context: str) -> None:
    if _find_missing(value):
        raise ValueError(f"{context} 中的打码值 {MASKED_SECRET!r} 没有可保留的旧值")


def _entry_for(record: ChannelRecord) -> ChannelConfigEntry:
    """Adapt a DB row to the pydantic shape the factories consume."""
    return ChannelConfigEntry(
        channel_id=record.channel_id,
        kind=record.kind,
        alias=record.alias,
        name=record.name,
        enabled=record.enabled,
        config=record.config,
        constraints=record.constraints,
    )


def _is_structural_change(record: ChannelRecord, fields: dict[str, Any]) -> bool:
    """True when a PATCH needs a manager remove+add (HANDOVER §13.4)."""
    for name in ("kind", "config", "constraints"):
        if name in fields and fields[name] != getattr(record, name):
            return True
    return "enabled" in fields and bool(fields["enabled"]) != record.enabled


def _json_safe(value: Any) -> Any:
    """Coerce arbitrary channel metadata to JSON-serializable primitives.

    Some channels (notably WeCom) park live SDK objects such as an open
    ``WSClient`` inside ``ChannelSubject.metadata``.  Those would make
    FastAPI's response encoder raise ``Unable to serialize unknown type``,
    so the control plane degrades them to their ``str()`` form.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    return str(value)


# ---------------------------------------------------------------------------
# GatewayRuntime
# ---------------------------------------------------------------------------


class GatewayRuntime:
    """Owns the ``ChannelManager`` and the per-channel runtime state.

    The runtime is created before the ASGI app is served and started from the
    FastAPI lifespan hook; all ``async`` channel operations therefore happen on
    the daemon loop (HANDOVER §11.2 #17).
    """

    def __init__(
        self,
        store: Store,
        *,
        token: str,
        manager: Any | None = None,
        processor: MessageProcessor | None = None,
        media_dir: Path | None = None,
        backend: BackendSpec | None = None,
    ) -> None:
        self.store = store
        self.token = token
        #: The single backend every turn is posted to (read-only for ``status``).
        self._backend = backend if backend is not None else resolve_backend_spec()
        self.media_dir = Path(media_dir) if media_dir is not None else paths.default_media_dir()
        self.events = EventBus()
        #: Warnings from the most recent :meth:`apply_config` call.
        self.last_config_warnings: list[str] = []

        self._manager = manager
        #: Built in :meth:`start` when not injected: ``build_processor`` reads
        #: settings, and the database has not necessarily been migrated yet.
        self._processor = processor
        self._registered: set[str] = set()
        self._states: dict[str, tuple[str, str | None]] = {}
        self._started = False
        self._stopping = False
        self._started_at: float | None = None

    # -- introspection ---------------------------------------------------------

    @property
    def manager(self) -> Any | None:
        """The upstream manager (``None`` until :meth:`start` builds one)."""
        return self._manager

    @property
    def processor(self) -> Any:
        """The ``MessageProcessor`` in use (built during :meth:`start` if absent)."""
        return self._processor

    @property
    def media_dir_path(self) -> Path:
        return self.media_dir

    @property
    def db_path(self) -> str:
        """SQLite file path, for ``/v1/status``."""
        return str(getattr(self.store, "_path", ""))

    @property
    def backend_url(self) -> str:
        """Effective backend base URL, for ``/v1/status`` (read-only)."""
        return self._backend.base_url

    @property
    def uptime_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.time() - self._started_at)

    def channel_state(self, channel_id: str) -> tuple[str, str | None]:
        """Return ``(state, last_error)`` for a channel.

        States are ``"running"``, ``"disabled"`` and ``"error"``.  Channels the
        runtime has never attempted to register report ``"disabled"``.
        """
        return self._states.get(channel_id, (STATE_DISABLED, None))

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Migrate the DB, build/start the manager and register enabled channels."""
        if self._started:
            return
        self.store.migrate()
        self._started_at = time.time()
        self.events.bind_loop(asyncio.get_running_loop())
        if self._processor is None:
            # Reads settings -> must happen after migrate().
            self._processor = build_processor(
                self.store,
                backend=self._backend,
                events=self.events,
            )
        if self._manager is None:
            self._manager = self._build_manager()
        self._started = True
        await self._manager.start()

        for record in self.store.list_channels(include_disabled=True):
            if not record.enabled:
                self._states[record.channel_id] = (STATE_DISABLED, None)
                continue
            try:
                await self._register(record)
            except Exception as exc:  # startup must not be blocked by one bad channel
                logger.exception("failed to register channel %r at startup", record.channel_id)
                self._set_state(record.channel_id, STATE_ERROR, str(exc))

    async def stop(self) -> None:
        """Drain in-flight turns, stop the manager and close the store.

        ``ChannelManager.stop()`` cancels workers without a grace period
        (HANDOVER §11.2 #7), so the runtime first waits for the processor's
        active turns to finish (bounded by ``settings.shutdown_grace_ms``).
        """
        if self._stopping or not self._started:
            return
        self._stopping = True
        try:
            if self._manager is not None:
                grace_ms = _positive_int(
                    self.store.get_setting("shutdown_grace_ms", _DEFAULT_SHUTDOWN_GRACE_MS),
                    _DEFAULT_SHUTDOWN_GRACE_MS,
                )
                idle = await self.wait_for_idle_turns(grace_ms / 1000)
                if not idle:
                    logger.warning(
                        "shutdown grace expired with %d turn(s) still active",
                        self.active_turns,
                    )
                try:
                    await self._manager.stop()
                except Exception:  # shutdown must continue even if one adapter hangs
                    logger.exception("failed to stop the channel manager")
            self._registered.clear()
            self.store.close()
            self._started = False
        finally:
            self._stopping = False

    @property
    def active_turns(self) -> int:
        """Number of turns currently being processed by the processor."""
        value = getattr(self._processor, "active_turns", 0)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0

    async def wait_for_idle_turns(self, timeout_s: float, *, poll_s: float = 0.05) -> bool:
        """Wait until no turn is active; ``False`` when the timeout expires."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while self.active_turns > 0:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_s)
        return True

    def _build_manager(self) -> Any:
        """Build the real upstream manager (lazy imports keep CLI startup fast)."""
        from octop_gateway import ChannelManager
        from octop_gateway.media import FileSystemMediaBackend

        workers = _positive_int(
            self.store.get_setting("workers_per_channel", _DEFAULT_WORKERS), _DEFAULT_WORKERS
        )
        queue_maxsize = _positive_int(
            self.store.get_setting("queue_maxsize", _DEFAULT_QUEUE_MAXSIZE), _DEFAULT_QUEUE_MAXSIZE
        )
        return ChannelManager(
            workers_per_channel=workers,
            queue_maxsize=queue_maxsize,
            media_backend=FileSystemMediaBackend(self.media_dir),
            processor=self._processor,
        )

    # -- state / registration helpers -----------------------------------------

    def _require_manager(self) -> Any:
        if self._manager is None:
            raise RuntimeError("gateway runtime is not started")
        return self._manager

    def _publish_state(self, channel_id: str, state: str, last_error: str | None = None) -> None:
        self.events.publish(
            {
                "type": "channel.state",
                "channel_id": channel_id,
                "state": state,
                "last_error": last_error,
                "ts": time.time(),
            }
        )

    def _set_state(self, channel_id: str, state: str, last_error: str | None = None) -> None:
        self._states[channel_id] = (state, last_error)
        self._publish_state(channel_id, state, last_error)

    async def _register(self, record: ChannelRecord) -> None:
        """Validate, register and start one channel; raises on failure."""
        manager = self._require_manager()
        config = build_config(record.kind, record.config)
        constraints = constraints_of(_entry_for(record))
        await manager.add_channel(
            channel_type=record.kind,
            config=config,
            channel_id=record.channel_id,
            constraints=constraints,
        )
        self._registered.add(record.channel_id)
        self.store.mark_channel_started(record.channel_id, time.time())
        self._set_state(record.channel_id, STATE_RUNNING)

    async def _unregister(self, channel_id: str) -> None:
        """Best-effort removal from the manager (upstream remove has no await gap)."""
        if self._manager is None or channel_id not in self._registered:
            self._registered.discard(channel_id)
            return
        try:
            await self._manager.remove_channel(channel_id)
        except Exception:  # removal is best-effort: DB truth must still be updated
            logger.exception("failed to remove channel %r from the manager", channel_id)
        finally:
            self._registered.discard(channel_id)

    # -- channel lookups -------------------------------------------------------

    def _require_channel(self, ref: str) -> ChannelRecord:
        record = self.store.get_channel(ref)
        if record is None:
            raise KeyError(ref)
        return record

    def _require_running(self, ref: str) -> ChannelRecord:
        record = self._require_channel(ref)
        if not record.enabled:
            raise ValueError(f"channel is disabled: {record.channel_id!r}")
        if record.channel_id not in self._registered:
            raise ValueError(f"channel is not running: {record.channel_id!r}")
        return record

    # -- channel operations ----------------------------------------------------

    async def add_channel(self, entry: ChannelConfigEntry) -> None:
        """Validate, insert and (when enabled) register a new channel.

        Manager failures are raised so the route layer can answer ``400``; the
        half-created DB row is rolled back, unlike startup registration.
        """
        self._require_manager()
        build_config(entry.kind, entry.config)
        constraints_of(entry)
        if self.store.get_channel(entry.channel_id) is not None:
            raise ValueError(f"channel_id 已存在:{entry.channel_id!r}")
        if entry.alias:
            alias_owner = self.store.get_channel(entry.alias)
            if alias_owner is not None and alias_owner.alias == entry.alias:
                raise ValueError(f"alias 已被占用:{entry.alias!r}")

        now = time.time()
        record = ChannelRecord(
            channel_id=entry.channel_id,
            kind=entry.kind,
            alias=entry.alias,
            name=entry.name,
            config=dict(entry.config),
            constraints=dict(entry.constraints),
            enabled=entry.enabled,
            created_at=now,
            updated_at=now,
        )
        self.store.add_channel(record)

        if not record.enabled:
            self._set_state(record.channel_id, STATE_DISABLED)
            self._audit("channel.add", record.channel_id, {"kind": record.kind, "enabled": False})
            return
        try:
            await self._register(record)
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.store.delete_channel(record.channel_id)
            self._states.pop(record.channel_id, None)
            self._set_state(record.channel_id, STATE_ERROR, str(exc))
            raise
        self._audit("channel.add", record.channel_id, {"kind": record.kind, "enabled": True})

    async def persist_login_credentials(self, channel_id: str, config: dict[str, Any]) -> None:
        """Persist credentials from a QR login before trying to start the channel.

        A transient registration failure (e.g. the freshly created Feishu app has
        not been published/approved yet) must **not** discard just-scanned
        credentials: store them with ``enabled=False`` first, then let the caller
        attempt enable separately.  The channel stays disabled on failure and can
        be reconnected with ``imgw channel login <ref>`` later.
        """
        record = self._require_channel(channel_id)
        merged = {**record.config, **config}
        await self._unregister(channel_id)
        self.store.update_channel(channel_id, config=merged, enabled=False)
        self._set_state(channel_id, STATE_DISABLED)
        self._audit(
            "channel.login",
            channel_id,
            {"kind": record.kind, "status": "credentials_saved"},
        )

    async def update_channel(self, ref: str, fields: dict[str, Any]) -> None:
        """Apply a PATCH.

        ``alias``/``name`` update in place; ``kind``/``config``/``constraints``/
        ``enabled`` go through remove+add because upstream has no update or
        restart API (HANDOVER §11.2 #9 / §13.4).
        """
        record = self._require_channel(ref)
        unknown = set(fields) - _CHANNEL_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"不支持的通道字段:{', '.join(sorted(unknown))}")
        if not fields:
            raise ValueError("没有要更新的字段")

        new_kind = fields.get("kind", record.kind)
        new_config = fields.get("config", record.config)
        new_constraints = fields.get("constraints", record.constraints)
        if not isinstance(new_kind, str):
            raise ValueError("kind 必须是字符串")
        if not isinstance(new_config, dict):
            raise ValueError("config 必须是 JSON 对象")
        if "config" in fields:
            # PATCH config is a *partial* update: ``"***"`` keeps the stored value
            # (same rule as config import, HANDOVER §13.4) and keys not mentioned
            # are preserved.  A masked value with no old value is a hard error.
            resolved = _resolve_masked(new_config, record.config)
            _raise_if_missing(resolved, "config")
            new_config = {**record.config, **resolved}
            fields["config"] = new_config
        if not isinstance(new_constraints, dict):
            raise ValueError("constraints 必须是 JSON 对象")
        build_config(new_kind, new_config)
        constraints_of(
            ChannelConfigEntry(
                channel_id=record.channel_id,
                kind=new_kind,
                constraints=new_constraints,
            )
        )

        if not _is_structural_change(record, fields):
            self.store.update_channel(record.channel_id, **fields)
            self._audit("channel.update", record.channel_id, {"fields": sorted(fields)})
            return

        await self._unregister(record.channel_id)
        try:
            updated = self.store.update_channel(record.channel_id, **fields)
        except Exception:
            # DB rejected the change after we stopped the old instance: put the
            # previous registration back so a failed PATCH is a no-op at runtime.
            if record.enabled:
                with contextlib.suppress(Exception):
                    await self._register(record)
            raise

        if not updated.enabled:
            self._set_state(updated.channel_id, STATE_DISABLED)
            self._audit("channel.update", updated.channel_id, {"fields": sorted(fields)})
            return
        try:
            await self._register(updated)
        except Exception as exc:
            # A failed re-registration must not leave an unstartable config in
            # the DB: restore the previous durable record and bring the old
            # instance back (PATCH is an interactive operation, unlike a config
            # document import which keeps the desired state).
            logger.exception("failed to re-register channel %r after an update", record.channel_id)
            try:
                self.store.update_channel(
                    record.channel_id,
                    kind=record.kind,
                    alias=record.alias,
                    name=record.name,
                    config=record.config,
                    constraints=record.constraints,
                    enabled=record.enabled,
                )
                if record.enabled:
                    await self._register(record)
                else:
                    self._set_state(record.channel_id, STATE_DISABLED)
            except Exception:
                logger.exception("failed to roll back channel %r after a failed update", record.channel_id)
                self._set_state(record.channel_id, STATE_ERROR, str(exc))
            raise ValueError(f"通道 {record.channel_id!r} 重新注册失败:{exc}") from exc
        self._audit("channel.update", updated.channel_id, {"fields": sorted(fields)})

    async def remove_channel(self, ref: str, *, purge: bool = False) -> None:
        """Stop, unregister and delete a channel; ``purge`` also drops its data."""
        record = self._require_channel(ref)
        await self._unregister(record.channel_id)
        deleted = self.store.delete_channel(record.channel_id, purge=purge)
        if not deleted:
            raise KeyError(ref)
        self._states.pop(record.channel_id, None)
        self._publish_state(record.channel_id, STATE_DISABLED)
        self._audit("channel.remove", record.channel_id, {"purge": purge})

    async def probe_channel(self, entry: ChannelConfigEntry) -> None:
        """Start/stop an ephemeral instance to validate credentials; no DB writes."""
        manager = self._require_manager()
        # Validate the kind / weixin shape up-front for a clean 400 ...
        build_config(entry.kind, entry.config)
        constraints_of(entry)
        # ... but hand upstream a plain dict: ``manager.probe_channel`` calls
        # ``dict(config)`` and runs the kind's ``from_dict`` (which knows how to
        # express weixin ``accounts``).
        await manager.probe_channel(
            entry.kind,
            dict(entry.config),
            channel_id=entry.channel_id,
        )

    # -- outbound messages -----------------------------------------------------

    async def push_text(self, ref: str, subject_id: str, text: str) -> None:
        """Push one text message to one subject of a running channel."""
        record = self._require_running(ref)
        manager = self._require_manager()
        subject = self._channel_subject(record.channel_id, subject_id)
        await manager.push_text(record.channel_id, subject, text)
        self._record_outbound(record, subject_id, text, source="push")
        self._audit(
            "message.push",
            record.channel_id,
            {"subject_id": subject_id, "chars": len(text)},
        )

    async def push_all(self, ref: str, text: str) -> tuple[int, int]:
        """Push to every known subject of a channel; returns ``(sent, failed)``.

        Targets come from the **durable** ``subjects`` table merged with the
        manager's in-memory registry: upstream keeps subjects in memory only, so
        a broadcast after a daemon restart would otherwise reach nobody
        (HANDOVER §11.2 #11).
        """
        record = self._require_running(ref)
        manager = self._require_manager()
        try:
            subject_ids = [str(item["subject_id"]) for item in self.list_subjects(record.channel_id)]
        except Exception:
            logger.exception("failed to list subjects for channel %r", record.channel_id)
            subject_ids = []

        sent = 0
        failed = 0
        seen: set[str] = set()
        for subject_id in subject_ids:
            if not subject_id or subject_id in seen:
                continue
            seen.add(subject_id)
            subject = self._channel_subject(record.channel_id, subject_id)
            try:
                await manager.push_text(record.channel_id, subject, text)
            except Exception:
                failed += 1
                logger.warning(
                    "broadcast push failed: channel=%s subject=%s",
                    record.channel_id,
                    subject_id,
                )
            else:
                sent += 1
                self._record_outbound(record, subject_id, text, source="broadcast")
        self._audit(
            "message.broadcast",
            record.channel_id,
            {"sent": sent, "failed": failed, "chars": len(text)},
        )
        return sent, failed

    def list_subjects(self, ref: str) -> list[dict[str, Any]]:
        """Known push targets, merging the DB registry with in-memory subjects."""
        record = self._require_channel(ref)
        rows = {row.subject_id: row for row in self.store.list_subjects(record.channel_id)}
        memory: dict[str, Any] = {}
        if self._manager is not None:
            try:
                memory = {
                    str(getattr(subject, "subject_id", "")): subject
                    for subject in self._manager.list_subjects(record.channel_id)
                }
            except Exception:
                logger.exception("failed to list in-memory subjects for %r", record.channel_id)
                memory = {}

        merged: list[dict[str, Any]] = []
        for subject_id, row in rows.items():
            live = memory.get(subject_id)
            merged.append(
                {
                    "subject_id": subject_id,
                    "chat_type": getattr(live, "chat_type", "") or row.chat_type,
                    "display_name": getattr(live, "display_name", "") or row.display_name,
                    "first_seen": row.first_seen,
                    "last_seen": getattr(live, "last_seen", None) or row.last_seen,
                    "metadata": _json_safe(dict(getattr(live, "metadata", {}) or row.metadata)),
                }
            )
        for subject_id, live in memory.items():
            if subject_id in rows:
                continue
            merged.append(
                {
                    "subject_id": subject_id,
                    "chat_type": getattr(live, "chat_type", ""),
                    "display_name": getattr(live, "display_name", ""),
                    "first_seen": getattr(live, "first_seen", None),
                    "last_seen": getattr(live, "last_seen", None),
                    "metadata": _json_safe(dict(getattr(live, "metadata", {}) or {})),
                }
            )
        merged.sort(key=lambda item: str(item["subject_id"]))
        return merged

    def _channel_subject(self, channel_id: str, subject_id: str) -> Any:
        """Rebuild a ``ChannelSubject`` from the durable DB row when possible."""
        from octop_gateway.models import ChannelSubject

        row = self.store.get_subject(channel_id, subject_id)
        if row is None:
            return ChannelSubject(subject_id=subject_id)
        return ChannelSubject(
            subject_id=subject_id,
            first_seen=row.first_seen,
            last_seen=row.last_seen,
            display_name=row.display_name,
            chat_type=row.chat_type,
            metadata=dict(row.metadata),
        )

    def _record_outbound(
        self,
        record: ChannelRecord,
        subject_id: str,
        text: str,
        *,
        source: str,
    ) -> None:
        now = time.time()
        message_id = self.store.add_message(
            MessageRecord(
                channel_id=record.channel_id,
                subject_id=subject_id,
                session_key=f"{record.channel_id}:{subject_id}",
                direction="out",
                kind="message",
                content=[{"type": "text", "text": text}],
                metadata={"source": source},
                platform_msg_id=None,
                ts=now,
                error=None,
            )
        )
        logger.debug("recorded outbound message id=%s channel=%s", message_id, record.channel_id)

    # -- config document import ------------------------------------------------

    async def apply_config(self, doc: ConfigDocument) -> dict[str, int]:
        """Validate and apply a whole config document.

        Validation happens completely before the first write (HANDOVER §13.8
        rule 8: ``config import`` is all-or-nothing).  Manager failures after
        the DB is written do **not** roll back the DB; they are reported as
        warnings and leave the affected channel in ``state="error"``.
        """
        self.last_config_warnings = []
        existing_channels = {record.channel_id: record for record in self.store.list_channels()}
        existing_settings = self.store.get_settings()
        existing_aliases = {
            record.alias: record.channel_id for record in existing_channels.values() if record.alias
        }

        # ---- validation phase: no writes below this line --------------------
        resolved_channels: list[tuple[ChannelConfigEntry, ChannelRecord | None, dict[str, Any]]] = []
        wanted_ids = {entry.channel_id for entry in doc.channels}
        for channel_entry in doc.channels:
            old_channel = existing_channels.get(channel_entry.channel_id)
            resolved_config = _resolve_masked(
                channel_entry.config,
                old_channel.config if old_channel is not None else _MISSING,
            )
            _raise_if_missing(resolved_config, f"channel {channel_entry.channel_id!r} config")
            if not isinstance(resolved_config, dict):
                raise ValueError(f"channel {channel_entry.channel_id!r} config 必须是 JSON 对象")
            build_config(channel_entry.kind, resolved_config)
            constraints_of(channel_entry)
            if channel_entry.alias:
                owner = existing_aliases.get(channel_entry.alias)
                if owner is not None and owner != channel_entry.channel_id and owner in wanted_ids:
                    raise ValueError(f"alias {channel_entry.alias!r} 已被通道 {owner!r} 占用")
            resolved_channels.append((channel_entry, old_channel, resolved_config))

        resolved_settings: dict[str, Any] = {}
        for key, raw_value in doc.settings.items():
            if isinstance(raw_value, str) and raw_value == MASKED_SECRET:
                continue
            resolved_setting = _resolve_masked(raw_value, existing_settings.get(key, _MISSING))
            _raise_if_missing(resolved_setting, f"setting {key!r}")
            try:
                json.dumps(resolved_setting, ensure_ascii=False)
            except TypeError as exc:
                raise ValueError(f"setting {key!r} 不是可序列化的 JSON: {exc}") from exc
            resolved_settings[key] = resolved_setting

        # ---- apply phase ----------------------------------------------------
        now = time.time()
        for channel_id in sorted(set(existing_channels) - wanted_ids):
            await self._unregister(channel_id)
            self._states.pop(channel_id, None)
            self.store.delete_channel(channel_id)
            self._publish_state(channel_id, STATE_DISABLED)

        for channel_entry, old_channel, resolved_config in resolved_channels:
            if old_channel is None:
                record = ChannelRecord(
                    channel_id=channel_entry.channel_id,
                    kind=channel_entry.kind,
                    alias=channel_entry.alias,
                    name=channel_entry.name,
                    config=resolved_config,
                    constraints=dict(channel_entry.constraints),
                    enabled=channel_entry.enabled,
                    created_at=now,
                    updated_at=now,
                )
                self.store.add_channel(record)
                if record.enabled:
                    await self._start_after_config_change(record)
                else:
                    self._set_state(record.channel_id, STATE_DISABLED)
                continue

            changed = (
                old_channel.kind != channel_entry.kind
                or old_channel.alias != channel_entry.alias
                or old_channel.name != channel_entry.name
                or old_channel.config != resolved_config
                or old_channel.constraints != dict(channel_entry.constraints)
                or old_channel.enabled != channel_entry.enabled
            )
            if not changed:
                # Recover a channel that previously failed to start even though
                # its configuration did not change.
                if channel_entry.enabled and channel_entry.channel_id not in self._registered:
                    await self._start_after_config_change(old_channel)
                continue

            runtime_relevant = (
                old_channel.kind != channel_entry.kind
                or old_channel.config != resolved_config
                or old_channel.constraints != dict(channel_entry.constraints)
                or old_channel.enabled != channel_entry.enabled
            )
            if runtime_relevant:
                await self._unregister(channel_entry.channel_id)
            updated = self.store.update_channel(
                channel_entry.channel_id,
                kind=channel_entry.kind,
                alias=channel_entry.alias,
                name=channel_entry.name,
                config=resolved_config,
                constraints=dict(channel_entry.constraints),
                enabled=channel_entry.enabled,
            )
            if not updated.enabled:
                self._set_state(updated.channel_id, STATE_DISABLED)
            elif runtime_relevant or updated.channel_id not in self._registered:
                # Alias/name-only edits keep the live instance untouched.
                await self._start_after_config_change(updated)

        for key, value in resolved_settings.items():
            self.store.set_setting(key, value)

        applied = {
            "channels": len(resolved_channels),
            "settings": len(resolved_settings),
        }
        self._audit("config.import", None, dict(applied))
        return applied

    async def _start_after_config_change(self, record: ChannelRecord) -> None:
        """Register a channel during import; failures become warnings, not errors."""
        try:
            await self._register(record)
        except Exception as exc:
            logger.exception("config import: failed to start channel %r", record.channel_id)
            self._set_state(record.channel_id, STATE_ERROR, str(exc))
            self.last_config_warnings.append(f"channel {record.channel_id!r} failed to start: {exc}")

    # -- audit -----------------------------------------------------------------

    def _audit(self, action: str, channel_id: str | None, payload: dict[str, Any]) -> None:
        """Write an audit row; payloads here never contain secrets."""
        try:
            self.store.add_audit(action, actor=_ACTOR, channel_id=channel_id, payload=payload)
        except Exception:  # auditing must never break the operation
            logger.exception("failed to write audit row for %s", action)
