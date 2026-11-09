"""Control-plane HTTP application (HANDOVER §13.6) and the daemon entry point.

The control plane is the **only** way to mutate a running daemon (HANDOVER
§13.8): the CLI is a pure HTTP client, so every operation here maps one-to-one
onto a :class:`~im_gateway.gateway.runtime.GatewayRuntime` call.

Wire conventions:

* ``/healthz`` and ``/readyz`` are the only unauthenticated endpoints (systemd
  and container probes);
* every other endpoint requires ``Authorization: Bearer <token>``;
* errors are always JSON ``{"detail": "..."}`` — ``400`` for bad input,
  ``404`` for unknown refs, ``500`` for unexpected failures;
* ``ref`` path/query/body values may be a ``channel_id`` or an ``alias`` and
  are resolved by the same ``Store.get_channel`` rule everywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from im_gateway import __version__
from im_gateway.core import paths
from im_gateway.core.capabilities import capabilities_for
from im_gateway.core.config import (
    MASKED_SECRET,
    ChannelConfigEntry,
    ConfigDocument,
)
from im_gateway.db.models import ChannelRecord, MessageRecord
from im_gateway.db.store import Store
from im_gateway.gateway.login import LoginManager
from im_gateway.gateway.maintenance import RetentionScheduler
from im_gateway.gateway.runtime import GatewayRuntime

__all__ = ["create_app", "run_daemon"]

logger = logging.getLogger(__name__)

#: Paths that must answer even without a bearer token (probes).
_PUBLIC_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/readyz"})
#: Keys whose string values are masked by ``config export`` by default.
_SECRET_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"secret|token|password|api_key|private_key", re.IGNORECASE
)
#: ``${ENV_VAR}`` references are portable and therefore never masked.
_ENV_REF_RE: Final[re.Pattern[str]] = re.compile(r"^\$\{[^}]+\}$")
_DEFAULT_TCP_HOST: Final[str] = "127.0.0.1"
_DEFAULT_TCP_PORT: Final[int] = 8765
_MAX_HISTORY_LIMIT: Final[int] = 1000


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ChannelUpdate(BaseModel):
    """Partial update for ``PATCH /v1/channels/{ref}``."""

    kind: str | None = None
    tenant_id: str | None = None
    alias: str | None = None
    name: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None


class MessagePush(BaseModel):
    """Body of ``POST /v1/messages``."""

    channel_id: str
    subject_id: str
    text: str


class MessageBroadcast(BaseModel):
    """Body of ``POST /v1/messages/broadcast``."""

    channel_id: str
    text: str


class MaintenanceRequest(BaseModel):
    """Body of ``POST /v1/db/maintenance``."""

    action: str


# ---------------------------------------------------------------------------
# View / export helpers
# ---------------------------------------------------------------------------


def _resolve_channel(store: Store, ref: str) -> ChannelRecord:
    """Resolve a ``channel_id`` or ``alias``; raise ``KeyError`` when unknown."""
    record = store.get_channel(ref)
    if record is None:
        raise KeyError(ref)
    return record


def _channel_view(runtime: GatewayRuntime, record: ChannelRecord) -> dict[str, Any]:
    state, last_error = runtime.channel_state(record.channel_id)
    return {
        "channel_id": record.channel_id,
        "kind": record.kind,
        "tenant_id": record.tenant_id,
        "alias": record.alias,
        "name": record.name,
        "enabled": record.enabled,
        "state": state,
        "last_error": last_error,
        "capabilities": capabilities_for(record.kind).model_dump(),
        "created_at": record.created_at,
        "last_started_at": record.last_started_at,
    }


def _message_view(record: MessageRecord) -> dict[str, Any]:
    return {
        "channel_id": record.channel_id,
        "subject_id": record.subject_id,
        "session_key": record.session_key,
        "direction": record.direction,
        "turn_id": record.turn_id,
        "kind": record.kind,
        "content": record.content,
        "metadata": record.metadata,
        "platform_msg_id": record.platform_msg_id,
        "ts": record.ts,
        "error": record.error,
    }


def _mask_secrets(value: Any) -> Any:
    """Recursively mask literal secrets; ``${ENV}`` references stay untouched."""
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if isinstance(raw_value, str) and _SECRET_KEY_RE.search(key) and not _ENV_REF_RE.match(raw_value):
                masked[key] = MASKED_SECRET
            else:
                masked[key] = _mask_secrets(raw_value)
        return masked
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def _export_document(store: Store, *, with_secrets: bool) -> dict[str, Any]:
    """Build the portable config document (HANDOVER §13.4)."""
    document: dict[str, Any] = {
        "version": 1,
        "channels": [
            {
                "channel_id": record.channel_id,
                "kind": record.kind,
                "tenant_id": record.tenant_id,
                "alias": record.alias,
                "name": record.name,
                "enabled": record.enabled,
                "config": dict(record.config),
                "constraints": dict(record.constraints),
            }
            for record in store.list_channels()
        ],
        "backends": [
            {
                "tenant_id": backend.tenant_id,
                "base_url": backend.base_url,
                "token_ref": backend.token_ref,
                "timeout_ms": backend.timeout_ms,
                "enabled": backend.enabled,
            }
            for backend in store.list_backends()
        ],
        "settings": store.get_settings(),
    }
    return document if with_secrets else _mask_secrets(document)


def _octop_version() -> str:
    try:
        return metadata.version("octop-gateway")
    except metadata.PackageNotFoundError:  # pragma: no cover - dependency is pinned
        return "unknown"


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Return the first exception inside (possibly nested) exception groups."""
    current = exc
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current


def _error_response(request: Request, exc: BaseException) -> JSONResponse:
    """Map a domain exception onto the control-plane JSON error shape."""
    from octop_gateway import ChannelCredentialsError

    root = _unwrap_exception(exc)
    if isinstance(root, ChannelCredentialsError):
        return JSONResponse({"detail": f"通道凭证无效: {root}"}, status_code=400)
    if isinstance(root, KeyError):
        target = root.args[0] if root.args else ""
        return JSONResponse({"detail": f"未找到: {target}"}, status_code=404)
    if isinstance(root, ValueError):
        return JSONResponse({"detail": str(root) or "请求无效"}, status_code=400)
    logger.error(
        "unhandled control-plane error on %s %s",
        request.method,
        request.url.path,
        exc_info=root,
    )
    return JSONResponse({"detail": "内部错误"}, status_code=500)


# ---------------------------------------------------------------------------
# Listen address parsing (bootstrap, HANDOVER §13.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ListenAddress:
    host: str | None = None
    port: int | None = None
    uds: Path | None = None


def _parse_listen_uri(raw: str) -> _ListenAddress:
    value = raw.strip()
    if value.startswith("unix://"):
        socket_path = value[len("unix://") :]
        if not socket_path:
            raise ValueError(f"invalid listen address {raw!r}: unix:// needs a socket path")
        return _ListenAddress(uds=Path(socket_path))
    if value.startswith("tcp://"):
        value = value[len("tcp://") :]
    host_part, separator, port_part = value.rpartition(":")
    if not separator or not host_part or not port_part:
        raise ValueError(f"invalid listen address {raw!r}: expected tcp://host:port or unix://path")
    try:
        port_number = int(port_part)
    except ValueError:
        raise ValueError(f"invalid listen address {raw!r}: port is not an integer") from None
    if not 0 < port_number < 65_536:
        raise ValueError(f"invalid listen address {raw!r}: port out of range")
    return _ListenAddress(host=host_part, port=port_number)


def _parse_listen_address(
    *,
    listen: str | None,
    host: str | None,
    port: int | None,
) -> _ListenAddress:
    """Priority: explicit ``listen`` > ``host``/``port`` > ``paths.default_listen``."""
    if listen and listen.strip():
        return _parse_listen_uri(listen)
    if host is not None or port is not None:
        return _ListenAddress(host=host or _DEFAULT_TCP_HOST, port=port or _DEFAULT_TCP_PORT)
    return _parse_listen_uri(paths.default_listen())


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    store: Store,
    runtime: GatewayRuntime | None = None,
    token: str,
    manager: Any | None = None,
) -> FastAPI:
    """Build the control-plane ASGI app.

    ``runtime`` may be injected (tests / embedding); otherwise one is built
    around ``store``.  ``manager`` is only used when the runtime is built here,
    which is how tests inject a fake upstream manager.
    """
    active_runtime = runtime if runtime is not None else GatewayRuntime(store, token=token, manager=manager)
    scheduler = RetentionScheduler(store)
    login_manager = LoginManager(active_runtime)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await active_runtime.start()
        await scheduler.start()  # catch-up retention pass + daily loop
        try:
            yield
        finally:
            await login_manager.close()
            await scheduler.stop()
            await active_runtime.stop()

    app = FastAPI(
        title="im-gateway-service",
        version=__version__,
        description="Control plane for the im-gateway-service daemon.",
        lifespan=lifespan,
    )
    app.state.runtime = active_runtime
    app.state.store = store
    app.state.token = token
    app.state.scheduler = scheduler
    app.state.login_manager = login_manager

    # -- auth --------------------------------------------------------------
    def _token_matches(provided: str) -> bool:
        if not provided:
            return False
        return secrets.compare_digest(provided.encode("utf-8"), token.encode("utf-8"))

    @app.middleware("http")
    async def _authenticate(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer" or not _token_matches(credentials.strip()):
            return JSONResponse(
                {"detail": "未授权:缺少或无效的 Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # -- error mapping -------------------------------------------------------
    @app.middleware("http")
    async def _map_errors(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Registered as HTTP middleware (outside Starlette's ExceptionMiddleware)
        # so domain exceptions become JSON responses *without* being re-raised
        # by ``ServerErrorMiddleware`` — which would surface them to TestClient
        # and to any other ASGI server as an internal error.
        try:
            return await call_next(request)
        except Exception as exc:
            return _error_response(request, exc)

    # -- probes / version / status ------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Any:
        try:
            store.table_counts()
        except Exception:  # DB must be readable for readiness
            logger.exception("readyz: database check failed")
            return JSONResponse({"detail": "数据库不可用"}, status_code=503)
        problems = []
        for record in store.list_channels(include_disabled=False):
            state, last_error = active_runtime.channel_state(record.channel_id)
            if state != "running":
                problems.append(
                    {
                        "channel_id": record.channel_id,
                        "state": state,
                        "last_error": last_error,
                    }
                )
        if problems:
            return JSONResponse({"detail": "存在未就绪的通道", "channels": problems}, status_code=503)
        return {"status": "ready"}

    @app.get("/v1/version")
    async def version() -> dict[str, str]:
        return {
            "service": __version__,
            "octop_gateway": _octop_version(),
            "python": platform.python_version(),
        }

    @app.get("/v1/status")
    async def status() -> dict[str, Any]:
        counts = store.table_counts()
        return {
            "service_version": __version__,
            "octop_gateway": _octop_version(),
            "uptime_s": round(active_runtime.uptime_s, 3),
            "db_path": active_runtime.db_path,
            "media_dir": str(active_runtime.media_dir),
            "channels": [_channel_view(active_runtime, record) for record in store.list_channels()],
            "backends": len(store.list_backends()),
            "counts": {
                "channels": counts.get("channels", 0),
                "messages": counts.get("messages", 0),
                "subjects": counts.get("subjects", 0),
            },
        }

    # -- channels ------------------------------------------------------------
    @app.get("/v1/channels")
    async def list_channels() -> dict[str, Any]:
        return {"channels": [_channel_view(active_runtime, record) for record in store.list_channels()]}

    @app.post("/v1/channels/probe")
    async def probe_channel(entry: ChannelConfigEntry) -> dict[str, Any]:
        await active_runtime.probe_channel(entry)
        return {"ok": True, "kind": entry.kind, "channel_id": entry.channel_id}

    @app.post("/v1/channels")
    async def add_channel(entry: ChannelConfigEntry) -> dict[str, Any]:
        await active_runtime.add_channel(entry)
        record = store.get_channel(entry.channel_id)
        if record is None:  # pragma: no cover - add_channel inserts before returning
            raise KeyError(entry.channel_id)
        return _channel_view(active_runtime, record)

    @app.get("/v1/channels/{ref}")
    async def get_channel(ref: str) -> dict[str, Any]:
        record = _resolve_channel(store, ref)
        return _channel_view(active_runtime, record)

    @app.patch("/v1/channels/{ref}")
    async def update_channel(ref: str, payload: ChannelUpdate) -> dict[str, Any]:
        record = _resolve_channel(store, ref)
        fields = payload.model_dump(exclude_unset=True)
        await active_runtime.update_channel(ref, fields)
        updated = store.get_channel(record.channel_id)
        if updated is None:  # pragma: no cover - update cannot delete the row
            raise KeyError(record.channel_id)
        return _channel_view(active_runtime, updated)

    @app.delete("/v1/channels/{ref}")
    async def remove_channel(ref: str, purge: bool = False) -> dict[str, Any]:
        record = _resolve_channel(store, ref)
        await active_runtime.remove_channel(ref, purge=purge)
        return {"deleted": True, "channel_id": record.channel_id, "purged": purge}

    @app.get("/v1/channels/{ref}/subjects")
    async def list_subjects(ref: str) -> dict[str, Any]:
        return {"subjects": active_runtime.list_subjects(ref)}

    # -- QR login (QQ / Weixin; HANDOVER §13.7, §11.4) ------------------------
    @app.post("/v1/channels/{ref}/login")
    async def start_login(ref: str) -> dict[str, Any]:
        return await login_manager.begin(ref)

    @app.get("/v1/channels/{ref}/login/{session_id}")
    async def poll_login(ref: str, session_id: str) -> dict[str, Any]:
        return await login_manager.poll(ref, session_id)

    @app.delete("/v1/channels/{ref}/login/{session_id}")
    async def cancel_login(ref: str, session_id: str) -> dict[str, Any]:
        return await login_manager.cancel(ref, session_id)

    # -- messages ------------------------------------------------------------
    @app.post("/v1/messages")
    async def push_message(payload: MessagePush, request: Request) -> dict[str, Any]:
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 请求头")
        record = _resolve_channel(store, payload.channel_id)
        if not store.claim_inbound(f"push:{idempotency_key}", record.channel_id):
            return {"sent": True, "deduplicated": True}
        await active_runtime.push_text(record.channel_id, payload.subject_id, payload.text)
        return {
            "sent": True,
            "deduplicated": False,
            "channel_id": record.channel_id,
            "subject_id": payload.subject_id,
        }

    @app.post("/v1/messages/broadcast")
    async def broadcast(payload: MessageBroadcast) -> dict[str, Any]:
        record = _resolve_channel(store, payload.channel_id)
        sent, failed = await active_runtime.push_all(record.channel_id, payload.text)
        return {"sent": sent, "failed": failed}

    # -- history / events ----------------------------------------------------
    @app.get("/v1/history")
    async def history(
        channel_id: str | None = None,
        subject_id: str | None = None,
        since: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        resolved_channel_id: str | None = None
        if channel_id:
            resolved_channel_id = _resolve_channel(store, channel_id).channel_id
        records = store.query_history(
            channel_id=resolved_channel_id,
            subject_id=subject_id,
            since=since,
            limit=max(1, min(limit, _MAX_HISTORY_LIMIT)),
            offset=max(0, offset),
        )
        return {"messages": [_message_view(record) for record in records]}

    @app.get("/v1/events")
    async def events(channel_id: str | None = None) -> Response:
        channel_filter: str | None = None
        if channel_id:
            record = store.get_channel(channel_id)
            channel_filter = record.channel_id if record is not None else channel_id

        async def _stream() -> AsyncIterator[str]:
            yield ": connected\n\n"
            async for event in active_runtime.events.subscribe():
                if channel_filter is not None and event.get("channel_id") != channel_filter:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- config export / import ----------------------------------------------
    @app.get("/v1/config/export")
    async def config_export(with_secrets: bool = False) -> dict[str, Any]:
        return _export_document(store, with_secrets=with_secrets)

    @app.post("/v1/config/import")
    async def config_import(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            document = ConfigDocument.model_validate(payload)
        except ValueError as exc:
            raise ValueError(f"配置文档无效:{exc}") from exc
        applied = await active_runtime.apply_config(document)
        return {"applied": applied, "warnings": list(active_runtime.last_config_warnings)}

    # -- DB maintenance ------------------------------------------------------
    @app.post("/v1/db/maintenance")
    async def db_maintenance(payload: MaintenanceRequest) -> dict[str, Any]:
        action = payload.action.strip().lower()
        if action == "prune":
            retention = store.get_setting("retention", {})
            if not isinstance(retention, dict):
                retention = {}
            deleted = await asyncio.to_thread(store.prune, retention)
            return {"action": "prune", "deleted": deleted}
        if action == "vacuum":
            await asyncio.to_thread(store.vacuum)
            return {"action": "vacuum", "ok": True}
        raise HTTPException(status_code=400, detail=f"不支持的维护操作:{payload.action!r}")

    return app


# ---------------------------------------------------------------------------
# Daemon entry point
# ---------------------------------------------------------------------------


def run_daemon(
    *,
    host: str | None = None,
    port: int | None = None,
    listen: str | None = None,
    db: str | None = None,
) -> None:
    """Run the control plane in the foreground (``imgw run``).

    This is deliberately the only function that starts a server; signal
    handling and graceful shutdown are uvicorn's/FastAPI's job (lifespan).
    """
    import uvicorn

    token = paths.load_or_create_token()
    db_path = Path(db) if db is not None else paths.default_db_path()
    store = Store(db_path)
    app = create_app(store=store, token=token)
    address = _parse_listen_address(listen=listen, host=host, port=port)
    if address.uds is not None:
        uvicorn.run(app, uds=str(address.uds))
    else:
        uvicorn.run(
            app,
            host=address.host or _DEFAULT_TCP_HOST,
            port=address.port or _DEFAULT_TCP_PORT,
        )
