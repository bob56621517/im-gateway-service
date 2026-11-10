"""Tests for the ``imgw`` control-plane CLI.

Everything runs against :class:`httpx.MockTransport`: no real HTTP server is
started, and for the command tests the real ``get_client`` is monkeypatched out
so the user's home-directory token file is never read. Token/URL resolution is
tested separately with ``IMGW_TOKEN_FILE`` pointed at ``tmp_path``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from im_gateway.cli import client as control_plane
from im_gateway.cli import main as cli
from im_gateway.cli import qr as qr_module

runner = CliRunner()

VALID_CONFIG = {
    "version": 2,
    "channels": [],
    "settings": {},
}


# ---------------------------------------------------------------------------
# Fake control plane
# ---------------------------------------------------------------------------


class FakeBackend:
    """Route table + request recorder backing an ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, Any, bytes | None]] = {}
        self.sequences: dict[tuple[str, str], list[tuple[int, Any]]] = {}
        self.requests: list[httpx.Request] = []

    def route(
        self,
        method: str,
        path: str,
        payload: Any,
        status_code: int = 200,
        *,
        content: bytes | None = None,
    ) -> None:
        self.routes[(method.upper(), path)] = (status_code, payload, content)

    def route_sequence(
        self,
        method: str,
        path: str,
        responses: list[tuple[int, Any]],
    ) -> None:
        """Serve different responses for repeated calls to the same route."""
        self.sequences[(method.upper(), path)] = list(responses)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method.upper(), request.url.path)
        sequence = self.sequences.get(key)
        if sequence:
            status_code, payload = sequence.pop(0)
            return httpx.Response(status_code, json=payload)
        if key not in self.routes:
            return httpx.Response(404, json={"detail": f"unexpected route {key[0]} {key[1]}"})
        status_code, payload, content = self.routes[key]
        if content is not None:
            return httpx.Response(
                status_code,
                content=content,
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(status_code, json=payload)

    def recorded(self, method: str, path: str) -> httpx.Request:
        for request in reversed(self.requests):
            if request.method.upper() == method and request.url.path == path:
                return request
        raise AssertionError(f"no request recorded for {method} {path}: {self.requests!r}")


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fake = FakeBackend()
    transport = httpx.MockTransport(fake.handle)

    def fake_get_client(url: str | None = None, token: str | None = None) -> httpx.Client:
        return httpx.Client(base_url="http://testserver", transport=transport)

    monkeypatch.setattr(cli, "get_client", fake_get_client)
    return fake


def channel_view(channel_id: str = "feishu:acme:sales", **overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "channel_id": channel_id,
        "kind": "feishu",
        "alias": None,
        "name": "",
        "enabled": True,
        "state": "running",
        "last_error": None,
        "login_supported": True,
        "credentials": {
            "configured": True,
            "required": ["app_id", "app_secret"],
            "missing": [],
        },
        "capabilities": {
            "streaming": "buffered",
            "markdown": "native",
            "media": {},
            "max_text_len": 4000,
            "native_ops": [],
        },
        "created_at": 1_700_000_000.0,
        "last_started_at": 1_700_000_100.0,
    }
    view.update(overrides)
    return view


def write_config(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Client: URL / token / transport resolution
# ---------------------------------------------------------------------------


def test_resolve_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMGW_URL", "http://env-host:1234")
    assert control_plane.resolve_url("http://explicit:9") == "http://explicit:9"
    assert control_plane.resolve_url() == "http://env-host:1234"


def test_resolve_url_normalises_tcp_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMGW_URL", raising=False)
    assert control_plane.resolve_url("tcp://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert control_plane.resolve_url("127.0.0.1:9999") == "http://127.0.0.1:9999"


def test_resolve_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMGW_URL", raising=False)
    monkeypatch.delenv("IMGW_LISTEN", raising=False)
    assert control_plane.resolve_url().startswith(("http://127.0.0.1:8765", "unix://"))


def test_resolve_token_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMGW_TOKEN", "env-token")
    assert control_plane.resolve_token("cli-token") == "cli-token"
    assert control_plane.resolve_token() == "env-token"


def test_resolve_token_reads_token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("IMGW_TOKEN", raising=False)
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(token_file))
    assert control_plane.resolve_token() == "file-token"


def test_resolve_token_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMGW_TOKEN", raising=False)
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(control_plane.ControlPlaneError, match="token"):
        control_plane.resolve_token()


def test_build_client_sets_bearer_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("IMGW_TOKEN", raising=False)
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(token_file))
    client = control_plane.build_client("http://127.0.0.1:9999")
    try:
        assert client.headers["Authorization"] == "Bearer file-token"
        assert str(client.base_url).startswith("http://127.0.0.1:9999")
    finally:
        client.close()


def test_build_client_uses_uds_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMGW_TOKEN", "t")
    captured: dict[str, str] = {}
    real_transport = httpx.HTTPTransport

    class RecordingTransport(real_transport):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["uds"] = kwargs.get("uds", "")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(control_plane.httpx, "HTTPTransport", RecordingTransport)
    client = control_plane.build_client("unix:///tmp/imgw.sock")
    try:
        assert captured["uds"] == "/tmp/imgw.sock"
        assert str(client.base_url).startswith("http://localhost")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# version / status / ready
# ---------------------------------------------------------------------------


def test_version_human_output(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/version", {"service": "0.1.0", "octop_gateway": "1.0.3", "python": "3.12.13"})
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0, result.stderr
    assert "0.1.0" in result.stdout
    assert "1.0.3" in result.stdout
    assert "3.12.13" in result.stdout


def test_version_json_output(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/version", {"service": "0.1.0", "octop_gateway": "1.0.3", "python": "3.12.13"})
    result = runner.invoke(cli.app, ["version", "--json"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["service"] == "0.1.0"


def test_version_json_global_flag(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/version", {"service": "0.1.0", "octop_gateway": "1.0.3", "python": "3.12.13"})
    result = runner.invoke(cli.app, ["--json", "version"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["octop_gateway"] == "1.0.3"


def test_status_human_output(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/status",
        {
            "service_version": "0.1.0",
            "octop_gateway": "1.0.3",
            "uptime_s": 65.0,
            "db_path": "/tmp/runtime.db",
            "media_dir": "/tmp/media",
            "backend_url": "http://127.0.0.1:8080",
            "channels": [channel_view()],
            "counts": {"channels": 1, "messages": 4, "subjects": 2},
        },
    )
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.stderr
    assert "feishu:acme:sales" in result.stdout
    assert "0.1.0" in result.stdout
    assert "1m 5s" in result.stdout
    assert "backend_url     http://127.0.0.1:8080" in result.stdout


def test_status_json_output(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/status",
        {
            "service_version": "0.1.0",
            "octop_gateway": "1.0.3",
            "uptime_s": 1.0,
            "db_path": "/tmp/runtime.db",
            "media_dir": "/tmp/media",
            "backend_url": "http://127.0.0.1:8080",
            "channels": [channel_view()],
            "counts": {"channels": 1},
        },
    )
    result = runner.invoke(cli.app, ["status", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["service_version"] == "0.1.0"
    assert payload["backend_url"] == "http://127.0.0.1:8080"
    assert payload["channels"][0]["channel_id"] == "feishu:acme:sales"


def test_ready_ok(backend: FakeBackend) -> None:
    backend.route("GET", "/readyz", {"status": "ready"})
    result = runner.invoke(cli.app, ["ready"])
    assert result.exit_code == 0, result.stderr
    assert "ready" in result.stdout


def test_ready_not_ready(backend: FakeBackend) -> None:
    backend.route("GET", "/readyz", {"detail": "database not ready"}, status_code=503)
    result = runner.invoke(cli.app, ["ready"])
    assert result.exit_code == 1
    assert "database not ready" in result.stderr


# ---------------------------------------------------------------------------
# channel commands
# ---------------------------------------------------------------------------


def test_channel_list_tsv(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels",
        {
            "channels": [
                channel_view(alias="sales"),
                channel_view(
                    "wecom:acme:ops",
                    kind="wecom",
                    credentials={
                        "configured": False,
                        "required": ["bot_id", "secret"],
                        "missing": ["bot_id", "secret"],
                    },
                ),
            ]
        },
    )
    result = runner.invoke(cli.app, ["channel", "list"])
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.rstrip("\n").split("\n")
    assert lines[0].split("\t") == ["channel_id", "kind", "state", "creds", "alias"]
    assert lines[1].split("\t") == ["feishu:acme:sales", "feishu", "running", "ok", "sales"]
    assert lines[2].split("\t") == ["wecom:acme:ops", "wecom", "running", "missing", "-"]


def test_channel_list_includes_disabled_and_header(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels",
        {"channels": [channel_view(), channel_view("feishu:acme:old", enabled=False, state="disabled")]},
    )
    plain = runner.invoke(cli.app, ["channel", "list"])
    assert plain.exit_code == 0, plain.stderr
    assert "feishu:acme:old" in plain.stdout  # disabled channels are shown by default
    assert plain.stdout.split("\n")[0] == "channel_id\tkind\tstate\tcreds\talias"
    bare = runner.invoke(cli.app, ["channel", "list", "--no-header"])
    assert bare.exit_code == 0, bare.stderr
    assert bare.stdout.split("\n")[0].startswith("feishu:acme:sales\t")


def test_channel_list_json(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels", {"channels": [channel_view()]})
    result = runner.invoke(cli.app, ["channel", "list", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["channels"][0]["channel_id"] == "feishu:acme:sales"


def test_channel_list_detail_masks_secrets(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales",
        channel_view(
            alias="sales",
            config={
                "app_id": "cli_x",
                "app_secret": "***",
                "nested": {"token": "***"},
            },
        ),
    )
    result = runner.invoke(cli.app, ["channel", "list", "feishu:acme:sales"])
    assert result.exit_code == 0, result.stderr
    assert "channel_id\tfeishu:acme:sales" in result.stdout
    assert "state\trunning" in result.stdout
    assert "credentials\tok" in result.stdout
    assert "config.app_id\tcli_x" in result.stdout
    assert "config.app_secret\t***" in result.stdout
    assert "config.nested.token\t***" in result.stdout


def test_terminal_qr_encoding_fallback() -> None:
    # Windows consoles default to GBK and cannot encode the half-block QR art.
    assert cli._stdout_can_encode("\u2580", encoding="utf-8") is True
    assert cli._stdout_can_encode("\u2580", encoding="gbk") is False
    assert cli._stdout_can_encode("URL: https://example.com", encoding="gbk") is True


def test_channel_login_silent_registers_new_channel(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels", channel_view())
    result = runner.invoke(
        cli.app,
        [
            "channel",
            "login",
            "feishu",
            "--id",
            "feishu:acme:sales",
            "--name",
            "sales",
            "--app-id",
            "cli_x",
            "--app-secret",
            "sekret",
        ],
    )
    assert result.exit_code == 0, result.stderr
    body = json.loads(backend.recorded("POST", "/v1/channels").content)
    assert body["channel_id"] == "feishu:acme:sales"
    assert body["kind"] == "feishu"
    assert body["alias"] == "sales"
    assert body["name"] == "sales"
    assert body["enabled"] is True
    assert body["config"] == {"app_id": "cli_x", "app_secret": "sekret"}
    assert "logged in" in result.stderr


def test_channel_login_check_probes_before_create(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels/probe", {"ok": True})
    backend.route("POST", "/v1/channels", channel_view())
    result = runner.invoke(
        cli.app,
        [
            "channel",
            "login",
            "feishu",
            "--id",
            "feishu:acme:sales",
            "--app-id",
            "cli_x",
            "--app-secret",
            "s",
            "--check",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert [(request.method, request.url.path) for request in backend.requests] == [
        ("GET", "/v1/channels/feishu"),
        ("POST", "/v1/channels/probe"),
        ("POST", "/v1/channels"),
    ]
    assert json.loads(backend.requests[1].content) == json.loads(backend.requests[2].content)


def test_channel_login_new_channel_qr_writes_png(
    backend: FakeBackend, tmp_path: Path
) -> None:
    backend.route("GET", "/v1/channels/feishu:acme:sales", channel_view())
    backend.route("POST", "/v1/channels", channel_view(enabled=False, state="disabled"))
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {"session_id": "s1", "status": "pending", "qr_content": "https://qr.example/x"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/login/s1",
        {"session_id": "s1", "status": "confirmed"},
    )
    qr_png = tmp_path / "qr.png"
    result = runner.invoke(
        cli.app,
        [
            "channel",
            "login",
            "feishu",
            "--id",
            "feishu:acme:sales",
            "--qr-png",
            str(qr_png),
            "--interval",
            "0.01",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert qr_png.read_bytes().startswith(b"\x89PNG")
    assert "login confirmed" in result.stderr
    assert "logged in" in result.stderr


def test_channel_login_url_only_no_wait(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/feishu:acme:sales", channel_view(credentials={"configured": False}))
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {"session_id": "s1", "status": "pending", "qr_content": "https://qr.example/x"},
    )
    result = runner.invoke(
        cli.app,
        ["channel", "login", "feishu:acme:sales", "--format", "url", "--no-wait"],
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "https://qr.example/x"
    assert "session: s1" in result.stderr


def test_channel_login_existing_reconnects_without_qr(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/feishu:acme:sales", channel_view())
    backend.route("PATCH", "/v1/channels/feishu:acme:sales", channel_view())
    result = runner.invoke(cli.app, ["channel", "login", "feishu:acme:sales"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(backend.recorded("PATCH", "/v1/channels/feishu:acme:sales").content) == {
        "enabled": True
    }
    assert "logged in" in result.stderr


def test_channel_login_existing_overwrites_credentials(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/feishu:acme:sales", channel_view())
    backend.route("PATCH", "/v1/channels/feishu:acme:sales", channel_view())
    result = runner.invoke(
        cli.app,
        ["channel", "login", "feishu:acme:sales", "--app-secret", "new-secret"],
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(backend.recorded("PATCH", "/v1/channels/feishu:acme:sales").content) == {
        "config": {"app_secret": "new-secret"},
        "enabled": True,
    }


def test_channel_login_rescan_forces_qr(backend: FakeBackend) -> None:
    backend.route_sequence(
        "GET",
        "/v1/channels/feishu:acme:sales",
        [(200, channel_view()), (200, channel_view())],
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {"session_id": "s1", "status": "pending", "qr_content": "https://qr.example/x"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/login/s1",
        {"session_id": "s1", "status": "confirmed"},
    )
    result = runner.invoke(
        cli.app, ["channel", "login", "feishu:acme:sales", "--rescan", "--interval", "0.01"]
    )
    assert result.exit_code == 0, result.stderr
    assert "login confirmed" in result.stderr
    for request in backend.requests:
        assert request.method != "PATCH"


def test_channel_login_missing_credentials_starts_qr(backend: FakeBackend) -> None:
    backend.route_sequence(
        "GET",
        "/v1/channels/wecom:acme:ops",
        [
            (200, channel_view("wecom:acme:ops", kind="wecom", credentials={"configured": False})),
            (200, channel_view("wecom:acme:ops", kind="wecom")),
        ],
    )
    backend.route(
        "POST",
        "/v1/channels/wecom:acme:ops/login",
        {"session_id": "s1", "status": "pending", "qr_content": "https://qr.example/w"},
    )
    backend.route(
        "GET",
        "/v1/channels/wecom:acme:ops/login/s1",
        {"session_id": "s1", "status": "confirmed"},
    )
    result = runner.invoke(
        cli.app, ["channel", "login", "wecom:acme:ops", "--interval", "0.01"]
    )
    assert result.exit_code == 0, result.stderr
    assert backend.recorded("POST", "/v1/channels/wecom:acme:ops/login") is not None


def test_channel_logout_disables(backend: FakeBackend) -> None:
    backend.route("PATCH", "/v1/channels/feishu:acme:sales", channel_view(enabled=False, state="disabled"))
    result = runner.invoke(cli.app, ["channel", "logout", "feishu:acme:sales"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(backend.recorded("PATCH", "/v1/channels/feishu:acme:sales").content) == {
        "enabled": False
    }
    assert "logged out" in result.stderr
    assert "credentials and history kept" in result.stderr


def test_channel_delete_requires_yes_when_not_tty(backend: FakeBackend) -> None:
    result = runner.invoke(cli.app, ["channel", "delete", "feishu:acme:sales"])
    assert result.exit_code == 1
    assert "--yes" in result.stderr
    assert backend.requests == []


def test_channel_delete_requires_logout_first(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/feishu:acme:sales", channel_view(enabled=True))
    result = runner.invoke(cli.app, ["channel", "delete", "feishu:acme:sales", "--yes"])
    assert result.exit_code == 1
    assert "logout" in result.stderr
    with pytest.raises(AssertionError):
        backend.recorded("DELETE", "/v1/channels/feishu:acme:sales")


def test_channel_delete_after_logout_purge(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/feishu:acme:sales", channel_view(enabled=False, state="disabled"))
    backend.route("DELETE", "/v1/channels/feishu:acme:sales", {"deleted": True, "purged": True})
    result = runner.invoke(
        cli.app, ["channel", "delete", "feishu:acme:sales", "--purge", "--yes"]
    )
    assert result.exit_code == 0, result.stderr
    request = backend.recorded("DELETE", "/v1/channels/feishu:acme:sales")
    assert dict(request.url.params) == {"purge": "true"}
    assert "deleted" in result.stderr
    assert "purged" in result.stderr


def test_channel_test_probe_ok(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels/probe", {"ok": True})
    result = runner.invoke(cli.app, ["channel", "test", "feishu", "--config", "app_id=x"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(backend.recorded("POST", "/v1/channels/probe").content)
    assert body["kind"] == "feishu"
    assert body["config"] == {"app_id": "x"}
    assert "probe ok: feishu" in result.stdout


def test_channel_test_probe_failure_exits_one(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels/probe", {"detail": "invalid app_secret"}, status_code=400)
    result = runner.invoke(cli.app, ["channel", "test", "feishu", "--config", "app_id=x"])
    assert result.exit_code == 1
    assert "invalid app_secret" in result.stderr


# ---------------------------------------------------------------------------
# send / subject / history
# ---------------------------------------------------------------------------


def test_send_posts_with_idempotency_key(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/messages", {"sent": True})
    result = runner.invoke(cli.app, ["send", "feishu:acme:sales", "subject-1", "hello"])
    assert result.exit_code == 0, result.stderr
    request = backend.recorded("POST", "/v1/messages")
    key = request.headers["Idempotency-Key"]
    assert uuid.UUID(key).version == 4
    assert json.loads(request.content) == {
        "channel_id": "feishu:acme:sales",
        "subject_id": "subject-1",
        "text": "hello",
    }
    assert "sent to feishu:acme:sales/subject-1" in result.stdout


def test_send_generates_unique_idempotency_keys(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/messages", {"sent": True})
    runner.invoke(cli.app, ["send", "ref", "subject", "one"])
    runner.invoke(cli.app, ["send", "ref", "subject", "two"])
    keys = [request.headers["Idempotency-Key"] for request in backend.requests]
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_send_broadcast(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/messages/broadcast", {"sent": 2, "failed": 0})
    result = runner.invoke(cli.app, ["send", "feishu:acme:sales", "--all", "hello all"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(backend.recorded("POST", "/v1/messages/broadcast").content)
    assert body == {"channel_id": "feishu:acme:sales", "text": "hello all"}
    assert "sent=2 failed=0" in result.stdout


def test_subject_ls(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/subjects",
        {
            "subjects": [
                {
                    "subject_id": "ou_123",
                    "chat_type": "direct",
                    "display_name": "Alice",
                    "first_seen": 1_700_000_000.0,
                    "last_seen": 1_700_000_100.0,
                }
            ]
        },
    )
    result = runner.invoke(cli.app, ["subject", "ls", "feishu:acme:sales"])
    assert result.exit_code == 0, result.stderr
    assert "ou_123" in result.stdout
    assert "Alice" in result.stdout


def test_subject_ls_json(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/subjects",
        {"subjects": [{"subject_id": "ou_123", "chat_type": "direct", "display_name": "Alice"}]},
    )
    result = runner.invoke(cli.app, ["subject", "ls", "feishu:acme:sales", "--json"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["subjects"][0]["subject_id"] == "ou_123"


def test_history_passes_filters(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/history",
        {
            "messages": [
                {
                    "channel_id": "feishu:acme:sales",
                    "subject_id": "ou_123",
                    "direction": "in",
                    "kind": "inbound",
                    "content": [{"type": "text", "text": "hi there"}],
                    "ts": 1_700_000_000.0,
                    "error": None,
                }
            ]
        },
    )
    result = runner.invoke(
        cli.app,
        ["history", "--channel", "feishu:acme:sales", "--subject", "ou_123", "--limit", "10"],
    )
    assert result.exit_code == 0, result.stderr
    assert "hi there" in result.stdout
    params = dict(backend.recorded("GET", "/v1/history").url.params)
    assert params == {"limit": "10", "channel_id": "feishu:acme:sales", "subject_id": "ou_123"}


def test_history_json(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/history", {"messages": []})
    result = runner.invoke(cli.app, ["history", "--json"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"messages": []}


# ---------------------------------------------------------------------------
# events (SSE)
# ---------------------------------------------------------------------------


def test_events_prints_sse_data_lines(backend: FakeBackend) -> None:
    body = (
        b'data: {"kind": "message", "seq": 1}\n\n'
        b": ping\n\n"
        b'data: {"kind": "completed", "seq": 2}\n\n'
    )
    backend.route("GET", "/v1/events", None, content=body)
    result = runner.invoke(cli.app, ["events", "--channel", "feishu:acme:sales"])
    assert result.exit_code == 0, result.stderr
    assert '{"kind": "message", "seq": 1}' in result.stdout
    assert '{"kind": "completed", "seq": 2}' in result.stdout
    assert "ping" not in result.stdout
    params = dict(backend.recorded("GET", "/v1/events").url.params)
    assert params == {"channel_id": "feishu:acme:sales"}


def test_events_error_detail_exits_one(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/events",
        {"detail": "events endpoint disabled"},
        status_code=503,
        content=b'{"detail": "events endpoint disabled"}',
    )
    result = runner.invoke(cli.app, ["events"])
    assert result.exit_code == 1
    assert "events endpoint disabled" in result.stderr


# ---------------------------------------------------------------------------
# config commands
# ---------------------------------------------------------------------------


def test_config_validate_accepts_valid_document(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG)
    result = runner.invoke(cli.app, ["config", "validate", str(path)])
    assert result.exit_code == 0, result.stderr
    assert "config OK" in result.stdout


def test_config_validate_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = runner.invoke(cli.app, ["config", "validate", str(path)])
    assert result.exit_code == 1
    assert "invalid config document" in result.stderr


def test_config_validate_rejects_removed_v1_document(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "version": 1,
            "channels": [{"channel_id": "a", "kind": "feishu"}],
            "backends": [],
            "settings": {},
        },
    )
    result = runner.invoke(cli.app, ["config", "validate", str(path)])
    assert result.exit_code == 1
    assert "invalid config document" in result.stderr
    assert "tenant/backends" in result.stderr


def test_config_export_stdout(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/config/export", VALID_CONFIG)
    result = runner.invoke(cli.app, ["config", "export"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == VALID_CONFIG
    assert dict(backend.recorded("GET", "/v1/config/export").url.params) == {}


def test_config_export_with_secrets_query(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/config/export", VALID_CONFIG)
    result = runner.invoke(cli.app, ["config", "export", "--with-secrets"])
    assert result.exit_code == 0, result.stderr
    params = dict(backend.recorded("GET", "/v1/config/export").url.params)
    assert params == {"with_secrets": "true"}


def test_config_export_to_file(backend: FakeBackend, tmp_path: Path) -> None:
    backend.route("GET", "/v1/config/export", VALID_CONFIG)
    output = tmp_path / "exported.json"
    result = runner.invoke(cli.app, ["config", "export", "-o", str(output)])
    assert result.exit_code == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == VALID_CONFIG
    assert "exported config" in result.stdout


def test_config_import_posts_document(backend: FakeBackend, tmp_path: Path) -> None:
    backend.route("POST", "/v1/config/import", {"applied": {"channels": 0, "settings": 0}})
    path = write_config(tmp_path, VALID_CONFIG)
    result = runner.invoke(cli.app, ["config", "import", str(path)])
    assert result.exit_code == 0, result.stderr
    body = json.loads(backend.recorded("POST", "/v1/config/import").content)
    assert body["version"] == 2
    assert "backends" not in body
    assert "channels=0" in result.stdout


def test_config_import_rejects_bad_file_locally(backend: FakeBackend, tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = runner.invoke(cli.app, ["config", "import", str(path)])
    assert result.exit_code == 1
    assert "invalid config document" in result.stderr
    assert backend.requests == []


# ---------------------------------------------------------------------------
# doctor / errors / stubs / help
# ---------------------------------------------------------------------------


def test_doctor_all_checks_pass(backend: FakeBackend) -> None:
    backend.route("GET", "/healthz", {"status": "ok"})
    backend.route("GET", "/readyz", {"status": "ready"})
    backend.route("GET", "/v1/version", {"service": "0.1.0", "octop_gateway": "1.0.3", "python": "3.12"})
    backend.route(
        "GET",
        "/v1/status",
        {"service_version": "0.1.0", "octop_gateway": "1.0.3", "counts": {"channels": 2}},
    )
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.stderr
    assert "all checks passed" in result.stdout


def test_doctor_fails_when_readyz_fails(backend: FakeBackend) -> None:
    backend.route("GET", "/healthz", {"status": "ok"})
    backend.route("GET", "/readyz", {"detail": "db locked"}, status_code=503)
    backend.route("GET", "/v1/version", {"service": "0.1.0", "octop_gateway": "1.0.3", "python": "3.12"})
    backend.route(
        "GET",
        "/v1/status",
        {"service_version": "0.1.0", "octop_gateway": "1.0.3", "counts": {"channels": 2}},
    )
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "db locked" in result.stdout


def test_http_error_prints_detail(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/status", {"detail": "channel not found: nope"}, status_code=400)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 1
    assert "channel not found: nope" in result.stderr


def test_unreachable_daemon_hints_imgw_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(boom)

    def fake_get_client(url: str | None = None, token: str | None = None) -> httpx.Client:
        return httpx.Client(base_url="http://testserver", transport=transport)

    monkeypatch.setattr(cli, "get_client", fake_get_client)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 1
    assert "imgw run" in result.stderr


def test_service_commands_delegate_to_controller(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeServiceController:
        def __init__(self, *, platform: str | None = None, spec: object = None) -> None:
            self.platform = platform
            self.spec = spec

        def install(self, *, target: Path | None = None, dry_run: bool = False) -> list[list[str]]:
            return [["systemctl", "--user", "daemon-reload"]]

        def start(self, *, dry_run: bool = False) -> list[list[str]]:
            return [["systemctl", "--user", "start", "im-gateway"]]

        def stop(self, *, dry_run: bool = False) -> list[list[str]]:
            return [["systemctl", "--user", "stop", "im-gateway"]]

        def restart(self, *, dry_run: bool = False) -> list[list[str]]:
            return [["systemctl", "--user", "restart", "im-gateway"]]

    monkeypatch.setattr(cli, "ServiceController", FakeServiceController)
    for command, expected in (
        ("install-service", "systemctl --user daemon-reload"),
        ("start", "systemctl --user start im-gateway"),
        ("stop", "systemctl --user stop im-gateway"),
        ("restart", "systemctl --user restart im-gateway"),
    ):
        result = runner.invoke(cli.app, [command])
        assert result.exit_code == 0, (command, result.stderr)
        assert f"$ {expected}" in result.stdout, (command, result.stdout)
    assert backend.requests == []


def test_help_lists_every_command() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.stderr
    for command in (
        "version",
        "status",
        "ready",
        "channel",
        "send",
        "subject",
        "history",
        "config",
        "events",
        "doctor",
        "run",
        "install-service",
        "start",
        "stop",
        "restart",
    ):
        assert command in result.stdout, command


def test_channel_help_lists_subcommands() -> None:
    result = runner.invoke(cli.app, ["channel", "--help"])
    assert result.exit_code == 0, result.stderr
    for command in ("login", "logout", "list", "delete", "test"):
        assert command in result.stdout, command
    assert "logout" in result.stdout
    assert "Bind" in result.stdout


# ---------------------------------------------------------------------------
# QR rendering (used by `imgw channel login`, milestone 2)
# ---------------------------------------------------------------------------


def test_render_qr_returns_terminal_art() -> None:
    rendered = qr_module.render_qr("https://example.com/login?token=abc")
    assert "█" in rendered
    assert len(rendered.splitlines()) > 5


def test_render_qr_bytes_is_png() -> None:
    assert qr_module.render_qr_bytes("https://example.com/login?token=abc").startswith(
        b"\x89PNG\r\n\x1a\n"
    )


def test_render_qr_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="empty"):
        qr_module.render_qr("")
    with pytest.raises(ValueError, match="empty"):
        qr_module.render_qr_bytes("")
