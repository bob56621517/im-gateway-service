"""QR login orchestration tests (offline: adapters and upstream helpers are faked)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from im_gateway.cli import main as cli_main
from im_gateway.db.models import ChannelRecord
from im_gateway.db.store import Store
from im_gateway.gateway.app import create_app
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.login import (
    SUPPORTED_LOGIN_KINDS,
    LoginChallenge,
    LoginManager,
    LoginPoll,
    QQLoginAdapter,
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

    async def start(self) -> LoginChallenge:
        self.started += 1
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

    async def update_channel(self, channel_id: str, fields: dict[str, Any]) -> None:
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
            tenant_id="acme",
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
    add_channel(store, channel_id="feishu:acme:1", kind="feishu")
    manager = make_manager(store)

    with pytest.raises(ValueError, match="不支持扫码登录"):
        await manager.begin("feishu:acme:1")
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
    channel_id, fields = runtime.updates[0]
    assert channel_id == CHANNEL_ID
    assert fields["config"] == {"sandbox": True, "app_id": "app_1", "app_secret": "sec_1"}
    audit = store.list_audit(limit=10)
    assert audit[0]["action"] == "channel.login"
    assert audit[0]["payload"] == {"kind": "qq", "status": "confirmed"}
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

    accounts = runtime.updates[0][1]["config"]["accounts"]
    assert accounts == [
        {"account_id": "a1", "token": "new"},
        {"account_id": "a2", "token": "second"},
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
    assert result.config_updates == {"app_id": "app", "app_secret": "sec"}

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

    async def wait_for_login(self, qrcode: str, on_scanned: Any = None) -> Any:
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

    challenge = await adapter.start()
    assert challenge.qr_image_url == "https://qr/img"
    await asyncio.sleep(0)
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


def test_default_adapter_factory() -> None:
    assert isinstance(default_adapter_factory("qq"), QQLoginAdapter)
    assert isinstance(default_adapter_factory("weixin"), WeixinLoginAdapter)
    with pytest.raises(ValueError, match="不支持扫码登录"):
        default_adapter_factory("feishu")


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
                tenant_id="acme",
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
                channel_id="feishu:acme:1",
                kind="feishu",
                tenant_id="acme",
                alias="fs",
                name="Feishu",
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
                tenant_id="acme",
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
        begin = client.post("/v1/channels/sales/login", headers=auth)
        assert begin.status_code == 200, begin.text
        session_id = begin.json()["session_id"]

        adapter.poll_results = [
            LoginPoll("success", config_updates={"app_id": "app_1", "app_secret": "sec_1"})
        ]
        poll = client.get(f"/v1/channels/sales/login/{session_id}", headers=auth)
        assert poll.status_code == 200
        assert poll.json()["status"] == "confirmed"

        record = login_manager._runtime.store.get_channel(CHANNEL_ID)
        assert record is not None
        assert record.config == {"app_id": "app_1", "app_secret": "sec_1"}


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
        unsupported = client.post("/v1/channels/fs/login", headers=auth)
        assert unsupported.status_code == 400
        assert "不支持扫码登录" in unsupported.json()["detail"]


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def test_cli_login_renders_qr_and_confirms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    calls: list[tuple[str, str]] = []

    def fake_api(
        client: Any,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Any = None,
        headers: Any = None,
    ) -> Any:
        calls.append((method, path))
        if method == "POST":
            return {
                "session_id": "s1",
                "channel_id": CHANNEL_ID,
                "kind": "qq",
                "status": "pending",
                "qr_content": "https://qr.example/abc",
                "qr_image_url": "",
            }
        return {"session_id": "s1", "channel_id": CHANNEL_ID, "status": "confirmed", "message": ""}

    @contextmanager
    def fake_client(*args: Any, **kwargs: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(cli_main, "get_client", fake_client)
    monkeypatch.setattr(cli_main.control_plane, "api", fake_api)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["channel", "login", "sales", "--interval", "0.01"])

    assert result.exit_code == 0, result.output
    assert "login confirmed: sales" in result.output
    assert calls[0][0] == "POST"
    assert calls[1] == ("GET", "/v1/channels/sales/login/s1")


def test_cli_login_expired_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    @contextmanager
    def fake_client(*args: Any, **kwargs: Any) -> Iterator[object]:
        yield object()

    def fake_api(client: Any, method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST":
            return {
                "session_id": "s1",
                "channel_id": CHANNEL_ID,
                "kind": "qq",
                "status": "pending",
                "qr_content": "https://qr.example/abc",
                "qr_image_url": "",
            }
        return {"session_id": "s1", "status": "expired", "message": "二维码已过期"}

    monkeypatch.setattr(cli_main, "get_client", fake_client)
    monkeypatch.setattr(cli_main.control_plane, "api", fake_api)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["channel", "login", "sales", "--interval", "0.01"])

    assert result.exit_code == 1
    assert "expired" in result.output.lower() or "二维码已过期" in result.output
