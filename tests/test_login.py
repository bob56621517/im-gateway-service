"""QR login orchestration tests (offline: adapters and upstream helpers are faked)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from im_gateway.db.models import ChannelRecord
from im_gateway.db.store import Store
from im_gateway.gateway.app import create_app
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.login import (
    FEISHU_LOGIN_ADDONS,
    SUPPORTED_LOGIN_KINDS,
    DingTalkLoginAdapter,
    FeishuLoginAdapter,
    LoginChallenge,
    LoginManager,
    LoginPoll,
    QQLoginAdapter,
    WeComLoginAdapter,
    WeixinLoginAdapter,
    default_adapter_factory,
)

TOKEN = "login-token"
CHANNEL_ID = "qq:acme:sales"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdapter:
    def __init__(self, *, challenge: LoginChallenge | None = None) -> None:
        self.challenge = challenge or LoginChallenge(qr_content="https://qr.example/abc")
        self.poll_results: list[LoginPoll] = []
        self.started = 0
        self.cancelled = 0
        self.raise_on_start: Exception | None = None
        self.start_timeouts: list[float | None] = []

    async def start(self, *, timeout_s: float | None = None) -> LoginChallenge:
        self.started += 1
        self.start_timeouts.append(timeout_s)
        if self.raise_on_start is not None:
            raise self.raise_on_start
        return self.challenge

    async def poll(self) -> LoginPoll:
        return self.poll_results.pop(0) if self.poll_results else LoginPoll("pending")

    async def cancel(self) -> None:
        self.cancelled += 1


class FakeRuntime:
    """Only the surface :class:`LoginManager` needs."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.events = EventBus()
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.persisted: list[tuple[str, dict[str, Any]]] = []
        self.fail_enable = False

    async def persist_login_credentials(
        self, channel_id: str, config: dict[str, Any]
    ) -> None:
        self.persisted.append((channel_id, config))

    async def update_channel(self, channel_id: str, fields: dict[str, Any]) -> None:
        if self.fail_enable and fields.get("enabled") is True:
            raise ValueError("Feishu token refresh failed")
        self.updates.append((channel_id, fields))


def add_channel(
    store: Store,
    *,
    channel_id: str = CHANNEL_ID,
    kind: str = "qq",
    config: dict[str, Any] | None = None,
    enabled: bool = False,
    alias: str = "sales",
) -> None:
    now = time.time()
    store.add_channel(
        ChannelRecord(
            channel_id=channel_id,
            kind=kind,
            alias=alias,
            name="Sales",
            config=dict(config or {}),
            constraints={},
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
    )


def make_manager(store: Store, adapters: dict[str, FakeAdapter] | None = None) -> LoginManager:
    runtime = FakeRuntime(store)

    def factory(kind: str) -> FakeAdapter:
        assert kind in SUPPORTED_LOGIN_KINDS
        if adapters is not None and kind in adapters:
            return adapters[kind]
        adapter = FakeAdapter()
        if adapters is not None:
            adapters[kind] = adapter
        return adapter

    return LoginManager(runtime, adapter_factory=factory)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LoginManager state machine
# ---------------------------------------------------------------------------


async def test_begin_returns_pending_challenge(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    adapters: dict[str, FakeAdapter] = {}
    manager = make_manager(store, adapters)

    view = await manager.begin("sales")

    assert view["status"] == "pending"
    assert view["channel_id"] == CHANNEL_ID
    assert view["kind"] == "qq"
    assert view["qr_content"] == "https://qr.example/abc"
    assert view["session_id"] in manager.sessions
    assert adapters["qq"].started == 1


async def test_begin_rejects_unsupported_kind_and_unknown_ref(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store, channel_id="telegram:acme:1", kind="telegram")
    manager = make_manager(store)

    with pytest.raises(ValueError, match="不支持扫码登录"):
        await manager.begin("telegram:acme:1")
    with pytest.raises(KeyError):
        await manager.begin("missing")


async def test_begin_wraps_adapter_failure(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    adapter = FakeAdapter()
    adapter.raise_on_start = RuntimeError("network down")
    manager = make_manager(store, {"qq": adapter})

    with pytest.raises(ValueError, match="登录初始化失败"):
        await manager.begin("sales")
    assert adapter.cancelled == 1
    assert manager.sessions == {}


async def test_poll_progresses_to_confirmed_and_applies_credentials(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store, config={"sandbox": True})
    adapter = FakeAdapter()
    adapter.poll_results = [
        LoginPoll("pending"),
        LoginPoll("scanned"),
        LoginPoll("success", config_updates={"app_id": "app_1", "app_secret": "sec_1"}),
    ]
    manager = make_manager(store, {"qq": adapter})
    runtime: FakeRuntime = manager._runtime  # type: ignore[assignment]

    view = await manager.begin("sales")
    session_id = str(view["session_id"])
    assert (await manager.poll("sales", session_id))["status"] == "pending"
    assert (await manager.poll("sales", session_id))["status"] == "scanned"
    confirmed = await manager.poll("sales", session_id)

    assert confirmed["status"] == "confirmed"
    assert runtime.persisted == [
        (CHANNEL_ID, {"sandbox": True, "app_id": "app_1", "app_secret": "sec_1"})
    ]
    assert runtime.updates == [(CHANNEL_ID, {"enabled": True})]
    audit = store.list_audit(limit=10)
    assert audit[0]["action"] == "channel.login"
    assert audit[0]["payload"] == {"kind": "qq", "status": "confirmed", "started": True}
    assert manager.sessions == {}
    with pytest.raises(KeyError):
        await manager.poll("sales", session_id)


async def test_poll_merges_weixin_accounts(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(
        store,
        channel_id="weixin:acme:cs",
        kind="weixin",
        config={"accounts": [{"account_id": "a1", "token": "old"}]},
    )
    adapter = FakeAdapter()
    adapter.poll_results = [
        LoginPoll(
            "success",
            config_updates={
                "accounts": [
                    {"account_id": "a1", "token": "new"},
                    {"account_id": "a2", "token": "second"},
                ]
            },
        )
    ]
    manager = make_manager(store, {"weixin": adapter})
    runtime: FakeRuntime = manager._runtime  # type: ignore[assignment]

    view = await manager.begin("weixin:acme:cs")
    await manager.poll("weixin:acme:cs", str(view["session_id"]))

    accounts = runtime.persisted[0][1]["accounts"]
    assert accounts == [
        {"account_id": "a1", "token": "new"},
        {"account_id": "a2", "token": "second"},
    ]


async def test_poll_keeps_credentials_when_channel_start_fails(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    adapter = FakeAdapter()
    adapter.poll_results = [
        LoginPoll("success", config_updates={"app_id": "app_1", "app_secret": "sec_1"})
    ]
    manager = make_manager(store, {"qq": adapter})
    runtime: FakeRuntime = manager._runtime  # type: ignore[assignment]
    runtime.fail_enable = True

    view = await manager.begin("sales")
    confirmed = await manager.poll("sales", str(view["session_id"]))

    assert confirmed["status"] == "confirmed"
    assert "凭据已保存" in confirmed["message"]
    assert runtime.persisted == [
        (CHANNEL_ID, {"app_id": "app_1", "app_secret": "sec_1"})
    ]


async def test_poll_expired_and_ttl_expiry(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    adapter = FakeAdapter()
    adapter.poll_results = [LoginPoll("expired", message="二维码过期")]
    clock = [0.0]
    manager = LoginManager(
        FakeRuntime(store),  # type: ignore[arg-type]
        adapter_factory=lambda kind: adapter,
        clock=lambda: clock[0],
        session_ttl_s=10,
    )

    first = await manager.begin("sales")
    expired = await manager.poll("sales", str(first["session_id"]))
    assert expired["status"] == "expired"
    assert expired["message"] == "二维码过期"

    second = await manager.begin("sales")
    clock[0] = 1000.0
    ttl = await manager.poll("sales", str(second["session_id"]))
    assert ttl["status"] == "expired"
    assert "超时" in ttl["message"]
    assert manager.sessions == {}


async def test_default_login_session_has_no_daemon_ttl(db_path: Path) -> None:
    """A pending login session lives until the platform QR or CLI timeout says otherwise."""
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    adapter = FakeAdapter(challenge=LoginChallenge(qr_content="x", expires_in_s=90))
    clock = [0.0]
    manager = LoginManager(
        FakeRuntime(store),  # type: ignore[arg-type]
        adapter_factory=lambda kind: adapter,
        clock=lambda: clock[0],
    )

    view = await manager.begin("sales", timeout_s=120)
    assert adapter.start_timeouts == [120]
    assert view["expires_in_s"] == 90
    assert view["status"] == "pending"

    clock[0] = 10_000.0  # way past any old 600s default
    still_pending = await manager.poll("sales", str(view["session_id"]))
    assert still_pending["status"] == "pending"


async def test_poll_and_cancel_scope_to_channel(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    add_channel(store, channel_id="qq:acme:other", alias="other")
    manager = make_manager(store)

    view = await manager.begin("sales")
    session_id = str(view["session_id"])
    with pytest.raises(KeyError):
        await manager.poll("other", session_id)
    with pytest.raises(KeyError):
        await manager.cancel("other", session_id)
    with pytest.raises(KeyError):
        await manager.poll("sales", "nope")

    cancelled = await manager.cancel("sales", session_id)
    assert cancelled["status"] == "cancelled"
    assert manager.sessions == {}


async def test_close_cancels_pending_sessions(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    add_channel(store)
    adapter = FakeAdapter()
    manager = make_manager(store, {"qq": adapter})

    await manager.begin("sales")
    await manager.close()

    assert adapter.cancelled == 1
    assert manager.sessions == {}


# ---------------------------------------------------------------------------
# Default adapters (upstream helpers faked; the mapping logic is ours)
# ---------------------------------------------------------------------------


class FakeQQLogin:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.poll_status = "pending"
        self.credentials: list[Any] = []

    async def fetch_qr_code(self) -> Any:
        return type("QR", (), {"task_id": "task-1", "qrcode_url": "https://qr/qq"})()

    async def poll(self, task_id: str) -> Any:
        return type(
            "Poll",
            (),
            {"status": self.poll_status, "credentials": self.credentials},
        )()

    def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)


async def test_qq_adapter_maps_upstream_results() -> None:
    login = FakeQQLogin()
    adapter = QQLoginAdapter(login=login)

    challenge = await adapter.start()
    assert challenge.qr_content == "https://qr/qq"
    assert (await adapter.poll()).status == "pending"

    login.poll_status = "success"
    login.credentials = [type("Cred", (), {"app_id": "app", "app_secret": "sec"})()]
    result = await adapter.poll()
    assert result.status == "success"
    assert result.config_updates == {"app_id": "app", "secret": "sec"}

    login.poll_status = "expired"
    assert (await adapter.poll()).status == "expired"

    await adapter.cancel()
    assert login.cancelled == ["task-1"]


async def test_qq_adapter_success_without_credentials_is_expired() -> None:
    login = FakeQQLogin()
    login.poll_status = "success"
    adapter = QQLoginAdapter(login=login)
    await adapter.start()

    result = await adapter.poll()

    assert result.status == "expired"
    assert "凭证" in result.message


class FakeWeixinLogin:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.gate: asyncio.Event | None = None
        self.scanned = False
        self.failure: Exception | None = None

    async def fetch_qr_code(self) -> Any:
        return type("QR", (), {"qrcode": "qrcode-token", "qrcode_img_content": "https://qr/img"})()

    async def wait_for_login(self, qrcode: str, on_scanned: Any = None, **kwargs: Any) -> Any:
        self.wait_timeout_s = kwargs.get("timeout_s")
        if on_scanned is not None:
            on_scanned()
            self.scanned = True
        if self.failure is not None:
            raise self.failure
        if self.gate is not None:
            await self.gate.wait()
        return type(
            "Wait",
            (),
            {
                "connected": self.connected,
                "bot_token": "bot-token",
                "account_id": "a1",
                "base_url": "https://base",
                "message": "" if self.connected else "二维码已过期",
            },
        )()


async def test_weixin_adapter_polls_scanned_then_confirmed() -> None:
    login = FakeWeixinLogin()
    login.gate = asyncio.Event()
    adapter = WeixinLoginAdapter(login=login)

    challenge = await adapter.start(timeout_s=123.0)
    assert challenge.qr_image_url == "https://qr/img"
    await asyncio.sleep(0)
    assert login.wait_timeout_s == 123.0  # --timeout is forwarded to wait_for_login
    assert (await adapter.poll()).status == "scanned"

    login.gate.set()
    await asyncio.sleep(0)
    result = await adapter.poll()
    assert result.status == "success"
    assert result.config_updates == {
        "accounts": [{"account_id": "a1", "token": "bot-token", "base_url": "https://base"}]
    }
    await adapter.cancel()


async def test_weixin_adapter_expired_and_failure() -> None:
    expired_login = FakeWeixinLogin(connected=False)
    expired = WeixinLoginAdapter(login=expired_login)
    await expired.start()
    await asyncio.sleep(0)
    result = await expired.poll()
    assert result.status == "expired"
    assert result.message == "二维码已过期"

    failing = FakeWeixinLogin()
    failing.failure = RuntimeError("boom")
    adapter = WeixinLoginAdapter(login=failing)
    await adapter.start()
    await asyncio.sleep(0)
    result = await adapter.poll()
    assert result.status == "expired"
    assert "boom" in result.message


# ---------------------------------------------------------------------------
# Feishu adapter (lark_oapi.register_app faked)
# ---------------------------------------------------------------------------


class AppAccessDeniedError(Exception):
    """Stands in for ``lark_oapi.scene.registration.errors.AppAccessDeniedError``."""

    code = "access_denied"


class AppExpiredError(Exception):
    """Stands in for ``lark_oapi.scene.registration.errors.AppExpiredError``."""

    code = "expired_token"


class FakeFeishuRegister:
    """Mimics ``register_app``: emits the QR URL, then blocks until released."""

    def __init__(self, *, url: str = "https://accounts.feishu.cn/qr/abc") -> None:
        self.url = url
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = {"client_id": "cli_x", "client_secret": "sec"}
        self.failure: BaseException | None = None
        self.gate: threading.Event | None = None
        self.qr_emitted = threading.Event()

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.url:
            kwargs["on_qr_code"]({"url": self.url, "expire_in": 600})
        self.qr_emitted.set()
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.failure is not None:
            raise self.failure
        return dict(self.result or {})


class BlockingFeishuRegister:
    """Never emits a QR URL; unblocks only when the adapter cancels it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cancel_requested = threading.Event()

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        cancel_event: threading.Event = kwargs["cancel_event"]
        if cancel_event.wait(5.0):
            self.cancel_requested.set()
            raise RuntimeError("cancelled by caller")
        raise RuntimeError("register timed out in test")


async def _poll_until_terminal(adapter: Any, *, timeout: float = 2.0) -> LoginPoll:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result: LoginPoll = await adapter.poll()
        if result.status != "pending":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("adapter did not reach a terminal state")


async def test_feishu_adapter_start_and_success() -> None:
    register = FakeFeishuRegister()
    adapter = FeishuLoginAdapter(register=register, source="imgw-test", qr_wait_s=2.0)

    challenge = await adapter.start()
    assert challenge.qr_content == "https://accounts.feishu.cn/qr/abc"
    assert challenge.expires_in_s == 600  # SDK-reported ``expire_in`` is surfaced

    assert len(register.calls) == 1
    kwargs = register.calls[0]
    assert kwargs["source"] == "imgw-test"
    assert kwargs["addons"] == FEISHU_LOGIN_ADDONS
    assert kwargs["create_only"] is True
    assert kwargs["app_preset"] == {"desc": "由 im-gateway 扫码创建"}
    assert isinstance(kwargs["cancel_event"], threading.Event)
    assert callable(kwargs["on_qr_code"])

    result = await _poll_until_terminal(adapter)
    assert result.status == "success"
    assert result.config_updates == {"app_id": "cli_x", "app_secret": "sec"}
    await adapter.cancel()


async def test_feishu_adapter_pending_then_success() -> None:
    register = FakeFeishuRegister()
    register.gate = threading.Event()
    adapter = FeishuLoginAdapter(register=register, qr_wait_s=2.0)

    await adapter.start()
    assert (await adapter.poll()).status == "pending"

    register.gate.set()
    result = await _poll_until_terminal(adapter)
    assert result.status == "success"
    assert result.config_updates == {"app_id": "cli_x", "app_secret": "sec"}
    await adapter.cancel()


async def test_feishu_adapter_success_without_credentials_is_expired() -> None:
    register = FakeFeishuRegister()
    register.result = {"client_id": "cli_x"}
    adapter = FeishuLoginAdapter(register=register, qr_wait_s=2.0)
    await adapter.start()

    result = await _poll_until_terminal(adapter)

    assert result.status == "expired"
    assert "凭证" in result.message
    await adapter.cancel()


async def test_feishu_adapter_failure_messages() -> None:
    cases: list[tuple[BaseException, str]] = [
        (AppAccessDeniedError("access_denied: denied"), "用户拒绝授权"),
        (AppExpiredError("expired_token: expired"), "二维码已过期"),
        (RuntimeError("boom"), "飞书扫码登录失败: boom"),
    ]
    for failure, expected in cases:
        register = FakeFeishuRegister()
        register.failure = failure
        adapter = FeishuLoginAdapter(register=register, qr_wait_s=2.0)
        await adapter.start()

        result = await _poll_until_terminal(adapter)

        assert result.status == "expired"
        assert expected in result.message
        await adapter.cancel()


async def test_feishu_adapter_start_timeout_and_early_failure() -> None:
    blocked = BlockingFeishuRegister()
    slow = FeishuLoginAdapter(register=blocked, qr_wait_s=0.1)
    with pytest.raises(RuntimeError, match="超时"):
        await slow.start()
    await slow.cancel()
    assert blocked.cancel_requested.is_set()

    def raising_register(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    failing = FeishuLoginAdapter(register=raising_register, qr_wait_s=1.0)
    with pytest.raises(RuntimeError, match="boom"):
        await failing.start()
    await failing.cancel()


# ---------------------------------------------------------------------------
# WeCom adapter (work.weixin.qq.com endpoints faked)
# ---------------------------------------------------------------------------


async def test_wecom_adapter_happy_path_and_pending() -> None:
    calls: list[tuple[str, str]] = []
    results: list[dict[str, Any]] = []

    async def generate(source: str) -> dict[str, Any]:
        calls.append(("generate", source))
        return {"scode": "scode-1", "auth_url": "https://work.weixin.qq.com/qr/abc"}

    async def poll(scode: str) -> dict[str, Any]:
        calls.append(("poll", scode))
        return results.pop(0) if results else {"status": ""}

    adapter = WeComLoginAdapter(generate=generate, poll=poll, source="imgw-test")

    challenge = await adapter.start()
    assert challenge.qr_content == "https://work.weixin.qq.com/qr/abc"
    assert calls[0] == ("generate", "imgw-test")

    results.append({"status": "pending"})
    assert (await adapter.poll()).status == "pending"
    assert calls[-1] == ("poll", "scode-1")

    results.append({"status": "success", "bot_id": "bot-1", "secret": "s3cr3t"})
    result = await adapter.poll()
    assert result.status == "success"
    assert result.config_updates == {"bot_id": "bot-1", "secret": "s3cr3t"}

    await adapter.cancel()


async def test_wecom_adapter_expired_and_failures() -> None:
    async def generate(source: str) -> dict[str, Any]:
        return {"scode": "sc", "auth_url": "https://work.weixin.qq.com/qr/abc"}

    async def expired(scode: str) -> dict[str, Any]:
        return {"status": "expired"}

    adapter = WeComLoginAdapter(generate=generate, poll=expired)
    await adapter.start()
    result = await adapter.poll()
    assert result.status == "expired"
    assert "过期" in result.message

    async def missing_secret(scode: str) -> dict[str, Any]:
        return {"status": "success", "bot_id": "bot-1"}

    missing = WeComLoginAdapter(generate=generate, poll=missing_secret)
    await missing.start()
    result = await missing.poll()
    assert result.status == "expired"
    assert "凭证" in result.message

    async def broken(scode: str) -> dict[str, Any]:
        raise RuntimeError("network down")

    failing = WeComLoginAdapter(generate=generate, poll=broken)
    await failing.start()
    result = await failing.poll()
    assert result.status == "expired"
    assert "network down" in result.message

    async def no_auth_url(source: str) -> dict[str, Any]:
        return {"scode": "sc"}

    with pytest.raises(RuntimeError, match="auth_url"):
        await WeComLoginAdapter(generate=no_auth_url).start()


async def test_wecom_adapter_cancel_clears_session() -> None:
    async def generate(source: str) -> dict[str, Any]:
        return {"scode": "sc", "auth_url": "https://work.weixin.qq.com/qr/abc"}

    async def pending(scode: str) -> dict[str, Any]:
        return {"status": "pending"}

    adapter = WeComLoginAdapter(generate=generate, poll=pending)
    await adapter.start()
    await adapter.cancel()
    assert (await adapter.poll()).status == "expired"


# ---------------------------------------------------------------------------
# DingTalk adapter (oapi.dingtalk.com endpoints faked)
# ---------------------------------------------------------------------------


class FakeDingTalkPost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.failure: Exception | None = None

    async def __call__(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, dict(payload)))
        if self.failure is not None:
            raise self.failure
        queue = self.responses.get(path)
        return dict(queue.pop(0)) if queue else {}

    def queue(self, path: str, *responses: dict[str, Any]) -> None:
        self.responses.setdefault(path, []).extend(responses)


def _dingtalk_post(*, expires_in: float = 60) -> FakeDingTalkPost:
    post = FakeDingTalkPost()
    post.queue("/app/registration/init", {"errcode": 0, "nonce": "nonce-1"})
    post.queue(
        "/app/registration/begin",
        {
            "errcode": 0,
            "device_code": "dev-1",
            "verification_uri_complete": "https://oapi.dingtalk.com/qr/abc",
            "expires_in": expires_in,
        },
    )
    return post


async def test_dingtalk_adapter_happy_path() -> None:
    post = _dingtalk_post()
    post.queue("/app/registration/poll", {"status": "WAITING"})
    clock = [100.0]
    adapter = DingTalkLoginAdapter(post=post, clock=lambda: clock[0])

    challenge = await adapter.start()
    assert challenge.qr_content == "https://oapi.dingtalk.com/qr/abc"
    assert challenge.expires_in_s == 60  # platform-reported ``expires_in``
    assert post.calls[0] == ("/app/registration/init", {})
    assert post.calls[1] == ("/app/registration/begin", {"nonce": "nonce-1"})

    assert (await adapter.poll()).status == "pending"
    assert post.calls[2] == ("/app/registration/poll", {"device_code": "dev-1"})

    post.queue(
        "/app/registration/poll",
        {"status": "SUCCESS", "client_id": "ding-id", "client_secret": "ding-sec"},
    )
    result = await adapter.poll()
    assert result.status == "success"
    assert result.config_updates == {"app_key": "ding-id", "app_secret": "ding-sec"}
    assert (await adapter.poll()).status == "expired"  # device code cleared


async def test_dingtalk_adapter_tolerates_provisioning_statuses() -> None:
    """Live API: WAITING → CREATING → PUBLISHING → SUCCESS (or APPROVING)."""
    post = _dingtalk_post()
    adapter = DingTalkLoginAdapter(post=post, clock=lambda: 0.0)
    await adapter.start()

    for status in ("SCANNED", "CREATING", "PUBLISHING"):
        post.queue("/app/registration/poll", {"status": status})
        assert (await adapter.poll()).status == "pending"

    post.queue("/app/registration/poll", {"status": "APPROVING"})  # no creds yet
    assert (await adapter.poll()).status == "pending"

    post.queue("/app/registration/poll", {"status": "SOME_FUTURE_STATE"})
    assert (await adapter.poll()).status == "pending"

    post.queue(
        "/app/registration/poll",
        {"status": "APPROVING", "client_id": "ding-id", "client_secret": "ding-sec"},
    )
    result = await adapter.poll()
    assert result.status == "success"
    assert result.config_updates == {"app_key": "ding-id", "app_secret": "ding-sec"}
    assert "审批" in result.message


async def test_dingtalk_adapter_expired_and_failures() -> None:
    expired_post = _dingtalk_post(expires_in=60)
    clock = [0.0]
    timed_out = DingTalkLoginAdapter(post=expired_post, clock=lambda: clock[0])
    await timed_out.start()
    clock[0] = 1000.0
    result = await timed_out.poll()
    assert result.status == "expired"
    assert "过期" in result.message
    assert (await timed_out.poll()).status == "expired"  # session cleared

    fail_post = _dingtalk_post()
    fail_post.queue(
        "/app/registration/poll",
        {"status": "FAIL", "fail_reason": "user rejected"},
    )
    failing = DingTalkLoginAdapter(post=fail_post, clock=lambda: 0.0)
    await failing.start()
    result = await failing.poll()
    assert result.status == "expired"
    assert "user rejected" in result.message

    missing_post = _dingtalk_post()
    missing_post.queue("/app/registration/poll", {"status": "SUCCESS", "client_id": "ding-id"})
    missing = DingTalkLoginAdapter(post=missing_post, clock=lambda: 0.0)
    await missing.start()
    result = await missing.poll()
    assert result.status == "expired"
    assert "凭证" in result.message

    raising = _dingtalk_post()
    broken = DingTalkLoginAdapter(post=raising, clock=lambda: 0.0)
    await broken.start()
    raising.failure = RuntimeError("network down")
    result = await broken.poll()
    assert result.status == "expired"
    assert "network down" in result.message


async def test_dingtalk_adapter_cancel_clears_session() -> None:
    post = _dingtalk_post()
    post.queue("/app/registration/poll", {"status": "WAITING"})
    adapter = DingTalkLoginAdapter(post=post, clock=lambda: 0.0)
    await adapter.start()
    await adapter.cancel()
    assert (await adapter.poll()).status == "expired"


def test_default_adapter_factory() -> None:
    assert isinstance(default_adapter_factory("qq"), QQLoginAdapter)
    assert isinstance(default_adapter_factory("weixin"), WeixinLoginAdapter)
    assert isinstance(default_adapter_factory("feishu"), FeishuLoginAdapter)
    assert isinstance(default_adapter_factory("wecom"), WeComLoginAdapter)
    assert isinstance(default_adapter_factory("dingtalk"), DingTalkLoginAdapter)
    with pytest.raises(ValueError, match="不支持扫码登录"):
        default_adapter_factory("telegram")


# ---------------------------------------------------------------------------
# Control-plane API
# ---------------------------------------------------------------------------


class StubManager:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def add_channel(self, **kwargs: Any) -> str:
        self.added.append(str(kwargs.get("channel_id")))
        return str(kwargs.get("channel_id"))

    async def remove_channel(self, channel_id: str) -> None:
        self.removed.append(channel_id)


@contextmanager
def login_app(db_path: Path) -> Iterator[tuple[TestClient, LoginManager, FakeAdapter]]:
    store = Store(db_path)
    manager = StubManager()
    app = create_app(store=store, token=TOKEN, manager=manager)
    with TestClient(app) as client:
        login_manager: LoginManager = app.state.login_manager
        adapter = FakeAdapter()
        login_manager._adapter_factory = lambda kind: adapter  # type: ignore[assignment]
        store.add_channel(
            ChannelRecord(
                channel_id=CHANNEL_ID,
                kind="qq",
                alias="sales",
                name="Sales",
                config={},
                constraints={},
                enabled=False,
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        store.add_channel(
            ChannelRecord(
                channel_id="telegram:acme:1",
                kind="telegram",
                alias="tg",
                name="Telegram",
                config={},
                constraints={},
                enabled=False,
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        store.add_channel(
            ChannelRecord(
                channel_id="qq:acme:other",
                kind="qq",
                alias="other",
                name="Other",
                config={},
                constraints={},
                enabled=False,
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        yield client, login_manager, adapter


def test_api_login_flow_persists_credentials(db_path: Path) -> None:
    with login_app(db_path) as (client, login_manager, adapter):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        begin = client.post("/v1/channels/sales/login", json={"timeout_s": 120}, headers=auth)
        assert begin.status_code == 200, begin.text
        session_id = begin.json()["session_id"]
        assert adapter.start_timeouts == [120.0]  # CLI budget reaches the adapter

        assert (
            client.post("/v1/channels/sales/login", json={"timeout_s": 0}, headers=auth).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/channels/sales/login", json={"timeout_s": 1, "bogus": True}, headers=auth
            ).status_code
            == 422
        )

        adapter.poll_results = [
            LoginPoll("success", config_updates={"app_id": "app_1", "app_secret": "sec_1"})
        ]
        poll = client.get(f"/v1/channels/sales/login/{session_id}", headers=auth)
        assert poll.status_code == 200
        assert poll.json()["status"] == "confirmed"

        record = login_manager._runtime.store.get_channel(CHANNEL_ID)
        assert record is not None
        assert record.config == {"app_id": "app_1", "app_secret": "sec_1"}
        assert record.enabled is True


def test_api_login_errors(db_path: Path) -> None:
    with login_app(db_path) as (client, _login_manager, _adapter):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/v1/channels/missing/login", headers=auth).status_code == 404
        assert client.get("/v1/channels/sales/login/nope", headers=auth).status_code == 404
        # Bound to the channel: a session of `sales` cannot be polled via another ref.
        begin = client.post("/v1/channels/sales/login", headers=auth).json()
        assert (
            client.get(f"/v1/channels/other/login/{begin['session_id']}", headers=auth).status_code
            == 404
        )
        assert (
            client.delete(f"/v1/channels/sales/login/{begin['session_id']}", headers=auth).status_code
            == 200
        )
        unsupported = client.post("/v1/channels/tg/login", headers=auth)
        assert unsupported.status_code == 400
        assert "不支持扫码登录" in unsupported.json()["detail"]
