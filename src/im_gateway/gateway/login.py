"""QR login orchestration (QQ / Weixin).

The upstream library ships two-phase QR helpers (``QQBotQRLogin`` /
``WeixinQRLogin``) as *host-driven tools*: they never write credentials to disk
and are not part of the channel lifecycle (HANDOVER §11.4).  This module owns the
host side:

* one in-memory login session per attempt (status machine + TTL),
* adapter objects that wrap the upstream helpers behind a tiny interface,
* applying the confirmed credentials to the channel config through the runtime
  (``remove_channel`` + ``add_channel`` because upstream has no update API), and
* auditing the result.

Adapters are injectable, so the whole state machine is unit-tested offline; the
two default adapters wrap the upstream helpers lazily (their network calls are
only exercised on a real platform, which is explicitly out of scope for the unit
tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from im_gateway.db.models import ChannelRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from im_gateway.gateway.runtime import GatewayRuntime

__all__ = [
    "SUPPORTED_LOGIN_KINDS",
    "LoginAdapter",
    "LoginChallenge",
    "LoginManager",
    "LoginPoll",
    "QQLoginAdapter",
    "WeixinLoginAdapter",
    "default_adapter_factory",
]

logger = logging.getLogger(__name__)

#: Channel kinds that have a QR binding flow.
SUPPORTED_LOGIN_KINDS: frozenset[str] = frozenset({"qq", "weixin"})
#: Default lifetime of a pending login session.
DEFAULT_SESSION_TTL_S: float = 600.0

LoginStatus = Literal["pending", "scanned", "success", "expired"]


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    """What the caller must render/visit to complete the login."""

    qr_content: str = ""  # text to encode as a QR code (QQ)
    qr_image_url: str = ""  # URL of a QR image (Weixin)


@dataclass(frozen=True, slots=True)
class LoginPoll:
    """One poll result; ``config_updates`` is only set on success."""

    status: LoginStatus
    config_updates: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class LoginAdapter(Protocol):
    """Minimal interface over an upstream two-phase QR helper."""

    async def start(self) -> LoginChallenge: ...

    async def poll(self) -> LoginPoll: ...

    async def cancel(self) -> None: ...


# ---------------------------------------------------------------------------
# Default adapters (lazy imports; injectable for tests)
# ---------------------------------------------------------------------------


class QQLoginAdapter:
    """Wrap ``octop_gateway.channels.qq.login_qr.QQBotQRLogin``."""

    def __init__(self, *, login: Any | None = None) -> None:
        self._login = login
        self._task_id: str = ""

    async def start(self) -> LoginChallenge:
        if self._login is None:
            from octop_gateway.channels.qq.login_qr import QQBotQRLogin

            self._login = QQBotQRLogin()
        response = await self._login.fetch_qr_code()
        self._task_id = str(response.task_id)
        return LoginChallenge(qr_content=str(response.qrcode_url))

    async def poll(self) -> LoginPoll:
        if self._login is None:
            return LoginPoll("expired", message="登录尚未初始化")
        result = await self._login.poll(self._task_id)
        status = str(result.status)
        if status == "success":
            credentials = list(result.credentials or [])
            if not credentials:
                return LoginPoll("expired", message="QQ 登录成功但未返回凭证")
            first = credentials[0]
            return LoginPoll(
                "success",
                config_updates={
                    "app_id": str(first.app_id),
                    "app_secret": str(first.app_secret),
                },
            )
        if status == "pending":
            return LoginPoll("pending")
        return LoginPoll("expired", message="二维码已过期，请重新登录")

    async def cancel(self) -> None:
        if self._login is not None and self._task_id:
            self._login.cancel(self._task_id)


class WeixinLoginAdapter:
    """Wrap ``octop_gateway.channels.weixin.login_qr.WeixinQRLogin``.

    Upstream only exposes a long-polling ``wait_for_login``; the adapter runs it
    as a background task so the control plane can answer ``poll`` immediately.
    """

    def __init__(self, *, login: Any | None = None) -> None:
        self._login = login
        self._qrcode: str = ""
        self._task: asyncio.Task[None] | None = None
        self._scanned = False
        self._result: LoginPoll | None = None

    async def start(self) -> LoginChallenge:
        if self._login is None:
            from octop_gateway.channels.weixin.login_qr import WeixinQRLogin

            self._login = WeixinQRLogin()
        response = await self._login.fetch_qr_code()
        self._qrcode = str(response.qrcode)
        self._task = asyncio.create_task(self._wait_for_login(), name="imgw-weixin-login")
        return LoginChallenge(qr_image_url=str(response.qrcode_img_content))

    async def _wait_for_login(self) -> None:
        if self._login is None:
            self._result = LoginPoll("expired", message="登录尚未初始化")
            return
        try:
            result = await self._login.wait_for_login(self._qrcode, on_scanned=self._on_scanned)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # adapter/network failure -> expired with reason
            logger.exception("weixin QR login failed")
            self._result = LoginPoll("expired", message=f"微信扫码登录失败: {exc}")
            return
        if not result.connected:
            self._result = LoginPoll("expired", message=str(result.message or "二维码已过期"))
            return
        self._result = LoginPoll(
            "success",
            config_updates={
                "accounts": [
                    {
                        "account_id": str(result.account_id or ""),
                        "token": str(result.bot_token or ""),
                        "base_url": str(result.base_url or ""),
                    }
                ]
            },
        )

    def _on_scanned(self) -> None:
        self._scanned = True

    async def poll(self) -> LoginPoll:
        if self._result is not None:
            return self._result
        if self._task is not None and self._task.done():
            # Task finished without setting a result: treat as expired.
            return LoginPoll("expired", message="微信扫码登录已结束")
        return LoginPoll("scanned" if self._scanned else "pending")

    async def cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def default_adapter_factory(kind: str) -> LoginAdapter:
    """Build the default adapter for a channel kind."""
    if kind == "qq":
        return QQLoginAdapter()
    if kind == "weixin":
        return WeixinLoginAdapter()
    raise ValueError(f"通道类型不支持扫码登录: {kind!r}")


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LoginSession:
    session_id: str
    channel_id: str
    kind: str
    adapter: LoginAdapter
    created_at: float
    status: LoginStatus = "pending"


class LoginManager:
    """Owns pending QR login sessions and applies confirmed credentials."""

    def __init__(
        self,
        runtime: GatewayRuntime,
        *,
        adapter_factory: Callable[[str], LoginAdapter] | None = None,
        clock: Callable[[], float] = time.time,
        session_ttl_s: float = DEFAULT_SESSION_TTL_S,
        actor: str = "api-token",
    ) -> None:
        self._runtime = runtime
        self._adapter_factory = adapter_factory or default_adapter_factory
        self._clock = clock
        self._session_ttl_s = max(1.0, float(session_ttl_s))
        self._actor = actor
        self._sessions: dict[str, _LoginSession] = {}

    @property
    def sessions(self) -> dict[str, _LoginSession]:
        """Read-only view for diagnostics/tests."""
        return dict(self._sessions)

    async def begin(self, ref: str) -> dict[str, Any]:
        """Start a login session and return the QR challenge."""
        record = self._require_channel(ref)
        if record.kind not in SUPPORTED_LOGIN_KINDS:
            raise ValueError(f"通道类型不支持扫码登录: {record.kind!r}")

        adapter = self._adapter_factory(record.kind)
        try:
            challenge = await adapter.start()
        except Exception as exc:  # surface a readable 400 instead of a 500
            with contextlib.suppress(Exception):
                await adapter.cancel()
            logger.exception("failed to start QR login: channel=%s", record.channel_id)
            raise ValueError(f"登录初始化失败: {exc}") from exc

        session_id = secrets.token_urlsafe(16)
        self._sessions[session_id] = _LoginSession(
            session_id=session_id,
            channel_id=record.channel_id,
            kind=record.kind,
            adapter=adapter,
            created_at=self._clock(),
        )
        return {
            "session_id": session_id,
            "channel_id": record.channel_id,
            "kind": record.kind,
            "status": "pending",
            "qr_content": challenge.qr_content,
            "qr_image_url": challenge.qr_image_url,
            "expires_in_s": self._session_ttl_s,
        }

    async def poll(self, ref: str, session_id: str) -> dict[str, Any]:
        """Advance one session; applies credentials on success."""
        record = self._require_channel(ref)
        session = self._require_session(record.channel_id, session_id)

        if self._clock() - session.created_at > self._session_ttl_s:
            await self._drop(session, cancel=True)
            return self._view(session, "expired", "登录会话已超时，请重新发起")

        result = await session.adapter.poll()
        if result.status == "success":
            await self._apply_credentials(record, result.config_updates)
            await self._drop(session, cancel=False)
            return self._view(session, "confirmed", result.message)
        if result.status == "expired":
            await self._drop(session, cancel=False)
            return self._view(session, "expired", result.message)
        session.status = result.status
        return self._view(session, result.status, result.message)

    async def cancel(self, ref: str, session_id: str) -> dict[str, Any]:
        """Abort a pending session (idempotent for unknown sessions -> 404)."""
        record = self._require_channel(ref)
        session = self._require_session(record.channel_id, session_id)
        await self._drop(session, cancel=True)
        return self._view(session, "cancelled", "登录已取消")

    async def close(self) -> None:
        """Cancel every pending session (called from the app lifespan)."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.adapter.cancel()

    # -- internals -------------------------------------------------------------

    def _require_channel(self, ref: str) -> ChannelRecord:
        record = self._runtime.store.get_channel(ref)
        if record is None:
            raise KeyError(ref)
        return record

    def _require_session(self, channel_id: str, session_id: str) -> _LoginSession:
        session = self._sessions.get(session_id)
        if session is None or session.channel_id != channel_id:
            raise KeyError(session_id)
        return session

    async def _drop(self, session: _LoginSession, *, cancel: bool) -> None:
        self._sessions.pop(session.session_id, None)
        if cancel:
            with contextlib.suppress(Exception):
                await session.adapter.cancel()

    async def _apply_credentials(self, record: ChannelRecord, updates: dict[str, Any]) -> None:
        """Merge confirmed credentials into the channel config and re-register."""
        config = dict(record.config)
        accounts = updates.get("accounts")
        if isinstance(accounts, list):
            config["accounts"] = self._merge_accounts(config.get("accounts"), accounts)
        for key, value in updates.items():
            if key != "accounts":
                config[key] = value
        await self._runtime.update_channel(record.channel_id, {"config": config})
        self._runtime.store.add_audit(
            "channel.login",
            actor=self._actor,
            channel_id=record.channel_id,
            payload={"kind": record.kind, "status": "confirmed"},
        )
        self._runtime.events.publish(
            {
                "type": "channel.login",
                "channel_id": record.channel_id,
                "kind": record.kind,
                "status": "confirmed",
                "ts": self._clock(),
            }
        )

    @staticmethod
    def _merge_accounts(existing: Any, incoming: list[Any]) -> list[Any]:
        merged: list[Any] = list(existing) if isinstance(existing, list) else []
        for new_account in incoming:
            if not isinstance(new_account, dict):
                continue
            account_id = str(new_account.get("account_id") or "")
            replaced = False
            for index, old_account in enumerate(merged):
                if isinstance(old_account, dict) and str(old_account.get("account_id") or "") == account_id:
                    merged[index] = new_account
                    replaced = True
                    break
            if not replaced:
                merged.append(new_account)
        return merged

    def _view(self, session: _LoginSession, status: str, message: str = "") -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "channel_id": session.channel_id,
            "kind": session.kind,
            "status": status,
            "message": message,
        }
