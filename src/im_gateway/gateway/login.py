"""QR login orchestration (QQ / Weixin / Feishu / WeCom / DingTalk).

The upstream library ships two-phase QR helpers (``QQBotQRLogin`` /
``WeixinQRLogin``) as *host-driven tools*: they never write credentials to disk
and are not part of the channel lifecycle (HANDOVER §11.4).  For the other three
platforms the adapters call their public scan-to-create endpoints directly
(Feishu through ``lark_oapi.register_app``).  This module owns the host side:

* one in-memory login session per attempt (status machine + TTL),
* adapter objects that wrap the platform flow behind a tiny interface,
* applying the confirmed credentials to the channel config through the runtime
  (``remove_channel`` + ``add_channel`` because upstream has no update API) and
  enabling the channel (a successful scan is also a bind), and
* auditing the result.

Adapters are injectable, so the whole state machine is unit-tested offline; the
default adapters import their SDK/networking dependencies lazily (their network
calls are only exercised on a real platform, which is explicitly out of scope
for the unit tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from im_gateway.db.models import ChannelRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from im_gateway.gateway.runtime import GatewayRuntime

__all__ = [
    "FEISHU_LOGIN_ADDONS",
    "SUPPORTED_LOGIN_KINDS",
    "DingTalkLoginAdapter",
    "FeishuLoginAdapter",
    "LoginAdapter",
    "LoginChallenge",
    "LoginManager",
    "LoginPoll",
    "QQLoginAdapter",
    "WeComLoginAdapter",
    "WeixinLoginAdapter",
    "default_adapter_factory",
]

logger = logging.getLogger(__name__)

#: Channel kinds that have a QR binding flow.
SUPPORTED_LOGIN_KINDS: frozenset[str] = frozenset({"qq", "weixin", "feishu", "wecom", "dingtalk"})
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


# ---------------------------------------------------------------------------
# Feishu adapter (scan to create an app; lark_oapi.register_app)
# ---------------------------------------------------------------------------

#: Scopes/events preset for a newly scanned Feishu app (HANDOVER §12.7 / §15.6).
#: ``register_app`` validates this payload before appending it to the QR URL, so
#: an invalid shape fails fast instead of producing a half-configured app.
FEISHU_LOGIN_ADDONS: dict[str, Any] = {
    "scopes": {
        "tenant": [
            "im:message:send_as_bot",
            "im:message.p2p_msg:readonly",
            "im:message.group_at_msg:readonly",
        ]
    },
    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
}

_FEISHU_APP_PRESET: dict[str, Any] = {"desc": "由 im-gateway 扫码创建"}


def _feishu_default_register(**kwargs: Any) -> dict[str, Any]:
    """Call ``lark_oapi.register_app`` (imported lazily: the SDK is heavy)."""
    import lark_oapi  # type: ignore[import-untyped]

    result: dict[str, Any] = lark_oapi.register_app(**kwargs)
    return result


def _feishu_warm_import() -> None:
    """Import ``lark_oapi`` off the event loop.

    The SDK imports every API module at top level; on a cold/slow filesystem that
    can take tens of seconds.  Warming it here keeps ``start()`` from timing out
    while it waits for the QR callback (the thread only starts afterwards).
    """
    __import__("lark_oapi")


def _feishu_failure_message(exc: BaseException) -> str:
    """Map a ``lark_oapi`` registration failure to a readable Chinese message.

    ``lark_oapi`` ships no type information (and importing it just to
    ``isinstance``-check exception classes would defeat the lazy import), so the
    public exception class names / ``code`` attribute are matched instead.  Both
    are part of the SDK's ``scene.registration.errors`` contract.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    code = str(getattr(exc, "code", ""))
    if "AppAccessDeniedError" in names or code == "access_denied":
        return "用户拒绝授权，请重新发起"
    if "AppExpiredError" in names or code == "expired_token":
        return "二维码已过期，请重新登录"
    return f"飞书扫码登录失败: {exc}"


class FeishuLoginAdapter:
    """Wrap ``lark_oapi.register_app`` (a blocking scan-to-create flow).

    ``register_app`` blocks until the user scans and the opened app is created;
    the adapter runs it on a daemon thread and hands the scanned QR URL back to
    ``start`` through a :class:`threading.Event`, so the event loop never blocks
    on the SDK.
    """

    def __init__(
        self,
        *,
        register: Callable[..., dict[str, Any]] | None = None,
        source: str | None = None,
        qr_wait_s: float = 60.0,
    ) -> None:
        self._register = register
        # ``source`` is the creation-page tracking tag (``source=python-sdk/<x>``).
        # Unknown tags may be denied by Feishu "platform policy", so the default
        # mirrors the source Octop registers with; override with
        # ``IMGW_FEISHU_REGISTER_SOURCE`` when testing.
        self._source = source or os.environ.get("IMGW_FEISHU_REGISTER_SOURCE", "").strip() or "octop"
        self._qr_wait_s = max(0.01, float(qr_wait_s))
        self._cancel_event = threading.Event()
        self._qr_ready = threading.Event()
        self._done = threading.Event()
        self._qr_payload: dict[str, Any] = {}
        self._result: dict[str, Any] | None = None
        self._error: BaseException | None = None
        self._error_message = ""
        self._thread: threading.Thread | None = None

    async def start(self) -> LoginChallenge:
        if self._thread is not None:
            raise RuntimeError("飞书扫码登录已在进行中，请先取消")
        if self._register is None:
            # Cold ``import lark_oapi`` is slow (tens of seconds); warm it before
            # the QR wait starts so the first scan does not spuriously time out.
            try:
                await asyncio.to_thread(_feishu_warm_import)
            except Exception as exc:
                raise RuntimeError(f"飞书 SDK 初始化失败: {exc}") from exc
        self._thread = threading.Thread(
            target=self._run_register,
            name="imgw-feishu-login",
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + self._qr_wait_s
        while not self._qr_ready.is_set():
            if self._done.is_set():
                message = self._error_message or "飞书扫码登录线程提前结束"
                raise RuntimeError(message)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
        if not self._qr_ready.is_set():
            raise RuntimeError(f"等待飞书二维码超时（{self._qr_wait_s:g}s），请重新发起")

        url = str(self._qr_payload.get("url") or "")
        if not url:
            raise RuntimeError("飞书扫码登录未返回二维码地址")
        return LoginChallenge(qr_content=url)

    def _run_register(self) -> None:
        register = self._register or _feishu_default_register
        try:
            result = register(
                on_qr_code=self._on_qr_code,
                source=self._source,
                addons=FEISHU_LOGIN_ADDONS,
                create_only=True,
                app_preset=dict(_FEISHU_APP_PRESET),
                cancel_event=self._cancel_event,
            )
        except BaseException as exc:  # recorded and surfaced via poll()
            self._error = exc
            self._error_message = _feishu_failure_message(exc)
        else:
            self._result = result
        finally:
            self._done.set()

    def _on_qr_code(self, info: dict[str, Any]) -> None:
        self._qr_payload = dict(info) if isinstance(info, dict) else {}
        self._qr_ready.set()

    async def poll(self) -> LoginPoll:
        if self._error is not None:
            return LoginPoll("expired", message=self._error_message or "飞书扫码登录失败")
        result = self._result
        if result is not None:
            client_id = str(result.get("client_id") or "")
            client_secret = str(result.get("client_secret") or "")
            if not client_id or not client_secret:
                return LoginPoll("expired", message="飞书扫码登录成功但未返回应用凭证")
            return LoginPoll(
                "success",
                config_updates={"app_id": client_id, "app_secret": client_secret},
            )
        if self._done.is_set():
            return LoginPoll("expired", message="飞书扫码登录已结束")
        return LoginPoll("pending")

    async def cancel(self) -> None:
        self._cancel_event.set()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 5.0)


# ---------------------------------------------------------------------------
# WeCom adapter (work.weixin.qq.com scan-to-create bot)
# ---------------------------------------------------------------------------

_WECOM_GENERATE_URL = "https://work.weixin.qq.com/ai/qc/generate"
_WECOM_QUERY_URL = "https://work.weixin.qq.com/ai/qc/query_result"


def _wecom_plat_code() -> int:
    """Platform code understood by the WeCom QR endpoint."""
    platform = sys.platform
    if platform.startswith("win"):
        return 2
    if platform == "darwin":
        return 1
    if platform.startswith("linux"):
        return 3
    return 0


async def _wecom_default_generate(source: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            _WECOM_GENERATE_URL,
            params={"source": source, "plat": _wecom_plat_code()},
        )
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("企业微信扫码接口未返回 data")
    if not data.get("scode") or not data.get("auth_url"):
        raise RuntimeError("企业微信扫码接口未返回 scode / auth_url")
    return data


async def _wecom_default_query(scode: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(_WECOM_QUERY_URL, params={"scode": scode})
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"status": ""}
    status = str(data.get("status") or "")
    if status != "success":
        return {"status": status}
    bot_info = data.get("bot_info")
    bot_info = bot_info if isinstance(bot_info, dict) else {}
    return {
        "status": "success",
        "bot_id": str(bot_info.get("botid") or ""),
        "secret": str(bot_info.get("secret") or ""),
    }


class WeComLoginAdapter:
    """Scan a WeCom QR code and return the created bot's credentials."""

    def __init__(
        self,
        *,
        generate: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        poll: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        source: str = "imgw",
    ) -> None:
        self._generate = generate
        self._poll_fn = poll
        self._source = source
        self._scode = ""

    async def start(self) -> LoginChallenge:
        generate = self._generate or _wecom_default_generate
        data = await generate(self._source)
        scode = str(data.get("scode") or "")
        auth_url = str(data.get("auth_url") or "")
        if not scode or not auth_url:
            raise RuntimeError("企业微信扫码接口未返回 scode / auth_url")
        self._scode = scode
        return LoginChallenge(qr_content=auth_url)

    async def poll(self) -> LoginPoll:
        if not self._scode:
            return LoginPoll("expired", message="登录尚未初始化")
        poll_fn = self._poll_fn or _wecom_default_query
        try:
            result = await poll_fn(self._scode)
        except Exception as exc:  # network/API failure -> expired with reason
            logger.exception("wecom QR login failed")
            return LoginPoll("expired", message=f"企业微信扫码登录失败: {exc}")
        status = str(result.get("status") or "")
        if status == "success":
            bot_id = str(result.get("bot_id") or "")
            secret = str(result.get("secret") or "")
            if not bot_id or not secret:
                return LoginPoll("expired", message="企业微信扫码成功但未返回机器人凭证")
            return LoginPoll("success", config_updates={"bot_id": bot_id, "secret": secret})
        if status in {"expired", "timeout"}:
            return LoginPoll("expired", message="二维码已过期，请重新登录")
        return LoginPoll("pending")

    async def cancel(self) -> None:
        self._scode = ""


# ---------------------------------------------------------------------------
# DingTalk adapter (oapi.dingtalk.com device-registration flow)
# ---------------------------------------------------------------------------

_DINGTALK_BASE_URL = "https://oapi.dingtalk.com"


async def _dingtalk_default_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=_DINGTALK_BASE_URL, timeout=15.0) as client:
        response = await client.post(path, json=payload)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("钉钉扫码接口返回格式异常")
    if data.get("errcode") not in {0, "0", None}:
        raise RuntimeError(f"钉钉扫码接口错误: {data.get('errmsg') or data.get('errcode')}")
    return data


def _positive_float(value: Any) -> float:
    """Best-effort ``expires_in`` parsing (missing/garbage -> no timeout)."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


class DingTalkLoginAdapter:
    """Scan a DingTalk QR code and return the created app's credentials."""

    def __init__(
        self,
        *,
        post: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._post = post
        self._clock = clock
        self._device_code = ""
        self._expires_at: float | None = None

    async def start(self) -> LoginChallenge:
        post = self._post or _dingtalk_default_post
        init = await post("/app/registration/init", {})
        nonce = str(init.get("nonce") or "")
        if not nonce:
            raise RuntimeError("钉钉扫码初始化未返回 nonce")
        begin = await post("/app/registration/begin", {"nonce": nonce})
        device_code = str(begin.get("device_code") or "")
        qr_url = str(begin.get("verification_uri_complete") or "")
        if not device_code or not qr_url:
            raise RuntimeError("钉钉扫码初始化未返回 device_code / verification_uri_complete")
        self._device_code = device_code
        expires_in = _positive_float(begin.get("expires_in"))
        self._expires_at = self._clock() + expires_in if expires_in else None
        return LoginChallenge(qr_content=qr_url)

    async def poll(self) -> LoginPoll:
        if not self._device_code:
            return LoginPoll("expired", message="登录尚未初始化")
        if self._expires_at is not None and self._clock() >= self._expires_at:
            self._clear()
            return LoginPoll("expired", message="二维码已过期，请重新登录")
        post = self._post or _dingtalk_default_post
        try:
            data = await post("/app/registration/poll", {"device_code": self._device_code})
        except Exception as exc:  # network/API failure -> expired with reason
            logger.exception("dingtalk QR login failed")
            self._clear()
            return LoginPoll("expired", message=f"钉钉扫码登录失败: {exc}")
        status = str(data.get("status") or "").upper()
        if status == "WAITING":
            return LoginPoll("pending")
        if status == "SUCCESS":
            self._clear()
            client_id = str(data.get("client_id") or "")
            client_secret = str(data.get("client_secret") or "")
            if not client_id or not client_secret:
                return LoginPoll("expired", message="钉钉扫码登录成功但未返回应用凭证")
            return LoginPoll(
                "success",
                config_updates={"app_key": client_id, "app_secret": client_secret},
            )
        self._clear()
        if status == "FAIL":
            reason = str(data.get("fail_reason") or data.get("fail_msg") or "")
            return LoginPoll("expired", message=f"钉钉扫码登录失败: {reason or '用户未授权'}")
        return LoginPoll("expired", message="二维码已过期，请重新登录")

    async def cancel(self) -> None:
        self._clear()

    def _clear(self) -> None:
        self._device_code = ""
        self._expires_at = None


def default_adapter_factory(kind: str) -> LoginAdapter:
    """Build the default adapter for a channel kind."""
    if kind == "qq":
        return QQLoginAdapter()
    if kind == "weixin":
        return WeixinLoginAdapter()
    if kind == "feishu":
        return FeishuLoginAdapter()
    if kind == "wecom":
        return WeComLoginAdapter()
    if kind == "dingtalk":
        return DingTalkLoginAdapter()
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
            warning = await self._apply_credentials(record, result.config_updates)
            await self._drop(session, cancel=False)
            return self._view(session, "confirmed", warning or result.message)
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

    async def _apply_credentials(self, record: ChannelRecord, updates: dict[str, Any]) -> str:
        """Merge confirmed credentials into the config, then try to enable.

        Credentials are persisted **first** (``enabled=False``): a transient
        start failure (a freshly scanned Feishu app can be blocked by platform
        policy until published) must not throw away what the user just scanned.
        Returns a user-facing warning when the channel could not be started.
        """
        config = dict(record.config)
        accounts = updates.get("accounts")
        if isinstance(accounts, list):
            config["accounts"] = self._merge_accounts(config.get("accounts"), accounts)
        for key, value in updates.items():
            if key != "accounts":
                config[key] = value
        await self._runtime.persist_login_credentials(record.channel_id, config)

        warning = ""
        try:
            await self._runtime.update_channel(record.channel_id, {"enabled": True})
        except Exception as exc:  # upstream/platform failures are data, not bugs
            logger.warning("login credentials saved but channel start failed: %s", exc)
            warning = (
                f"凭据已保存，但通道启动失败：{exc}。"
                f"修复后在后台执行 `imgw channel login {record.channel_id}` 重连。"
            )
        self._runtime.store.add_audit(
            "channel.login",
            actor=self._actor,
            channel_id=record.channel_id,
            payload={"kind": record.kind, "status": "confirmed", "started": not warning},
        )
        self._runtime.events.publish(
            {
                "type": "channel.login",
                "channel_id": record.channel_id,
                "kind": record.kind,
                "status": "confirmed",
                "started": not warning,
                "ts": self._clock(),
            }
        )
        return warning

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
