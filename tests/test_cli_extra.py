"""Extra CLI tests: uncovered branches of ``cli/client.py``, ``cli/main.py`` and
``cli/service.py``.

Everything is offline and deterministic: ``httpx.MockTransport`` stands in for
the control plane, ``get_client`` is monkeypatched so the user's token file is
never read, and system commands go through injected fake runners.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
import typer
from typer.testing import CliRunner

from im_gateway.cli import client as control_plane
from im_gateway.cli import main as cli
from im_gateway.cli import service
from im_gateway.cli.service import ServiceError, ServiceSpec

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake control plane (same shape as tests/test_cli.py, kept local on purpose)
# ---------------------------------------------------------------------------


class FakeBackend:
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

    def route_sequence(self, method: str, path: str, responses: list[tuple[int, Any]]) -> None:
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
            return httpx.Response(status_code, content=content)
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


def invoke(*args: str) -> Any:
    return runner.invoke(cli.app, list(args))


def context() -> click.Context:
    return click.Context(click.Command("test"))


# ---------------------------------------------------------------------------
# cli/client.py
# ---------------------------------------------------------------------------


def test_normalise_url_blank_returns_default() -> None:
    assert control_plane._normalise_url("   ") == control_plane.DEFAULT_URL
    assert control_plane._normalise_url("tcp://127.0.0.1:9000") == "http://127.0.0.1:9000"
    assert control_plane._normalise_url("localhost:9000") == "http://localhost:9000"
    assert control_plane._normalise_url("https://x") == "https://x"


def test_resolve_token_empty_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("IMGW_TOKEN", raising=False)
    with pytest.raises(control_plane.ControlPlaneError, match="empty"):
        control_plane.resolve_token()


def test_build_client_rejects_unix_without_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMGW_TOKEN", "token")
    with pytest.raises(control_plane.ControlPlaneError, match="socket path"):
        control_plane.build_client("unix://")


def _request(path: str = "/v1/test") -> httpx.Request:
    return httpx.Request("GET", f"http://testserver{path}")


def test_decode_response_empty_body_is_none() -> None:
    response = httpx.Response(204, request=_request())
    assert control_plane.decode_response(response) is None


def test_decode_response_non_json_body_raises() -> None:
    response = httpx.Response(200, content=b"<html>nope</html>", request=_request())
    with pytest.raises(control_plane.ControlPlaneError, match="non-JSON body"):
        control_plane.decode_response(response)


def test_decode_response_error_detail_variants() -> None:
    dict_detail = httpx.Response(400, json={"detail": {"field": "channel_id"}}, request=_request())
    with pytest.raises(control_plane.ControlPlaneError) as excinfo:
        control_plane.decode_response(dict_detail)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == '{"field": "channel_id"}'

    message = httpx.Response(404, json={"message": "not here"}, request=_request())
    with pytest.raises(control_plane.ControlPlaneError) as excinfo:
        control_plane.decode_response(message)
    assert excinfo.value.detail == "not here"

    html = httpx.Response(502, content=b"bad gateway", request=_request())
    with pytest.raises(control_plane.ControlPlaneError) as excinfo:
        control_plane.decode_response(html)
    assert excinfo.value.detail == "bad gateway"

    empty = httpx.Response(500, content=b"", request=_request())
    with pytest.raises(control_plane.ControlPlaneError) as excinfo:
        control_plane.decode_response(empty)
    assert "HTTP 500" in (excinfo.value.detail or "")


def test_error_detail_ignores_non_dict_json() -> None:
    response = httpx.Response(400, json=["not", "a", "dict"], request=_request())
    detail = control_plane._error_detail(response)
    assert "not" in detail
    assert detail == response.text.strip()


# ---------------------------------------------------------------------------
# cli/main.py: small helpers
# ---------------------------------------------------------------------------


def test_options_falls_back_to_defaults() -> None:
    ctx = context()
    options = cli._options(ctx)
    assert isinstance(options, cli.CliOptions)
    assert options.url is None
    assert cli._want_json(ctx) is False


def test_parse_config_pairs_rejects_malformed_pairs() -> None:
    assert cli._parse_config_pairs(["a=true", "b=FALSE", "c=42", "d=hello", "e=3.5"]) == {
        "a": True,
        "b": False,
        "c": 42,
        "d": "hello",
        "e": "3.5",
    }
    with pytest.raises(typer.BadParameter):
        cli._parse_config_pairs(["missing-equals"])
    with pytest.raises(typer.BadParameter):
        cli._parse_config_pairs(["=no-key"])


def test_formatting_helpers() -> None:
    assert cli._fmt_ts("not-a-number") == "-"
    assert cli._fmt_ts(0) != "-"
    assert cli._fmt_uptime("not-a-number") == "-"
    assert cli._fmt_uptime(90) == "1m 30s"
    assert cli._fmt_uptime(3_700) == "1h 1m 40s"
    assert cli._fmt_uptime(90_000) == "1d 1h 0m"
    assert cli._truncate("abcdef", 3) == "ab…"
    assert cli._content_text("raw text") == "raw text"
    assert cli._content_text(None) == ""
    assert cli._content_text([{"type": "image"}, {"other": 1}]) == ""
    assert cli._content_text([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]) == "one two"


def test_fmt_ts_handles_conversion_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(value: float) -> Any:
        raise OSError("timestamp out of range")

    monkeypatch.setattr(cli.time, "localtime", boom)
    assert cli._fmt_ts(1.0) == "-"


def test_current_username_falls_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise KeyError("no passwd entry")

    monkeypatch.setattr(cli.getpass, "getuser", boom)
    monkeypatch.setenv("USER", "fallback-user")
    assert cli._current_username() == "fallback-user"


def test_sse_data_helper() -> None:
    assert cli._sse_data("") is None
    assert cli._sse_data(": keepalive") is None
    assert cli._sse_data("event: message") is None
    assert cli._sse_data("data:") is None
    assert cli._sse_data('data: {"a": 1}') == '{"a": 1}'


class _ContextClient:
    def __enter__(self) -> _ContextClient:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_wait_ready_returns_when_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_client", lambda url=None, token=None: _ContextClient())
    monkeypatch.setattr(cli.control_plane, "api", lambda client, method, path: {"status": "ready"})
    cli._wait_ready(context())
    assert "ready: the daemon answered /readyz" in capsys.readouterr().out


def test_wait_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(client: Any, method: str, path: str) -> Any:
        raise control_plane.ControlPlaneError("connection refused")

    monkeypatch.setattr(cli, "get_client", lambda url=None, token=None: _ContextClient())
    monkeypatch.setattr(cli.control_plane, "api", unreachable)
    with pytest.raises(ServiceError, match="did not become ready"):
        cli._wait_ready(context(), timeout_s=0.0)


# ---------------------------------------------------------------------------
# cli/main.py: command branches
# ---------------------------------------------------------------------------


def test_send_all_missing_text_is_usage_error(backend: FakeBackend) -> None:
    assert invoke("send", "sales", "--all").exit_code == 2


def test_send_missing_subject_or_text_is_usage_error(backend: FakeBackend) -> None:
    assert invoke("send", "sales").exit_code == 2
    assert invoke("send", "sales", "user-1").exit_code == 2


def test_send_broadcast_json(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/messages/broadcast", {"sent": 2, "failed": 1})
    result = invoke("--json", "send", "sales", "--all", "hello")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"sent": 2, "failed": 1}


def test_send_single_json(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/messages", {"sent": True, "deduplicated": False})
    result = invoke("--json", "send", "sales", "user-1", "hello")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"sent": True, "deduplicated": False}
    body = json.loads(backend.recorded("POST", "/v1/messages").content)
    assert body == {"channel_id": "sales", "subject_id": "user-1", "text": "hello"}


def test_history_with_since_and_no_messages(backend: FakeBackend, capsys: pytest.CaptureFixture[str]) -> None:
    backend.route("GET", "/v1/history", {"messages": []})
    result = invoke("history", "--channel", "sales", "--since", "1.5")
    assert result.exit_code == 0, result.stderr
    assert "(no messages)" in result.stdout
    params = dict(backend.recorded("GET", "/v1/history").url.params)
    assert params["since"] == "1.5"
    assert params["channel_id"] == "sales"


class _KeyboardInterruptClient(_ContextClient):
    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt


class _TransportErrorClient(_ContextClient):
    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")


def test_events_keyboard_interrupt_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_client", lambda url=None, token=None: _KeyboardInterruptClient())
    result = invoke("events")
    assert result.exit_code == 0, result.stderr


def test_events_transport_error_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_client", lambda url=None, token=None: _TransportErrorClient())
    result = invoke("events")
    assert result.exit_code == 1
    assert "cannot reach" in result.stderr


def test_doctor_json_output(backend: FakeBackend) -> None:
    backend.route("GET", "/healthz", {"status": "ok"})
    backend.route("GET", "/readyz", {"status": "ready"})
    backend.route("GET", "/v1/version", {"service": "0.1.0", "octop_gateway": "1.0.3"})
    backend.route("GET", "/v1/status", {"service_version": "0.1.0", "counts": {"channels": 1}})
    result = invoke("--json", "doctor")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [check["check"] for check in payload["checks"]] == [
        "healthz",
        "readyz",
        "version",
        "status",
    ]


def test_run_command_delegates_to_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    from im_gateway.gateway import app as gateway_app

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway_app, "run_daemon", lambda **kwargs: calls.append(kwargs))
    result = invoke("run", "--host", "127.0.0.1", "--port", "9999")
    assert result.exit_code == 0, result.stderr
    assert calls == [{"host": "127.0.0.1", "port": 9999, "backend_url": None}]


def test_run_command_passes_backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from im_gateway.gateway import app as gateway_app

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway_app, "run_daemon", lambda **kwargs: calls.append(kwargs))
    result = invoke("run", "--backend-url", "http://127.0.0.1:7777")
    assert result.exit_code == 0, result.stderr
    assert calls == [
        {"host": "127.0.0.1", "port": 8765, "backend_url": "http://127.0.0.1:7777"}
    ]


def test_run_dev_command_delegates_with_dev_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from im_gateway.gateway import app as gateway_app

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway_app, "run_daemon", lambda **kwargs: calls.append(kwargs))
    result = invoke("run-dev", "--dev-backend-port", "9911")
    assert result.exit_code == 0, result.stderr
    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 8765,
            "dev_backend": True,
            "dev_backend_port": 9911,
        }
    ]


def test_channel_login_no_longer_accepts_tenant(backend: FakeBackend) -> None:
    result = invoke("channel", "login", "feishu", "--tenant", "acme")
    assert result.exit_code == 2
    assert "No such option" in result.stderr or "no such option" in result.stderr
    assert backend.requests == []


def test_channel_list_empty(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels", {"channels": []})
    result = invoke("channel", "list")
    assert result.exit_code == 0, result.stderr
    assert "(no channels" in result.stderr
    assert result.stdout == ""


def test_channel_list_detail_json(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels/sales",
        {"channel_id": "feishu:acme:sales", "kind": "feishu", "state": "running"},
    )
    result = invoke("--json", "channel", "list", "sales")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["channel_id"] == "feishu:acme:sales"


def test_channel_login_silent_json(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels", {"channel_id": "cli:added", "state": "running"})
    result = invoke(
        "--json",
        "channel",
        "login",
        "feishu",
        "--id",
        "cli:added",
        "--config",
        "app_id=cli",
        "--app-secret",
        "s",
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["event"] == "result"
    assert payload["channel_id"] == "cli:added"
    body = json.loads(backend.recorded("POST", "/v1/channels").content)
    assert body["config"] == {"app_id": "cli", "app_secret": "s"}


def test_channel_login_silent_qq_maps_secret(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels", {"channel_id": "qq:bot", "state": "disabled"})
    result = invoke(
        "--json",
        "channel",
        "login",
        "qq",
        "--id",
        "qq:bot",
        "--app-id",
        "1024",
        "--app-secret",
        "s3cr3t",
    )
    assert result.exit_code == 0, result.stderr
    body = json.loads(backend.recorded("POST", "/v1/channels").content)
    assert body["config"] == {"app_id": "1024", "secret": "s3cr3t"}


def test_channel_logout_json(backend: FakeBackend) -> None:
    backend.route("PATCH", "/v1/channels/sales", {"channel_id": "feishu:acme:sales"})
    result = invoke("--json", "channel", "logout", "sales")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["channel_id"] == "feishu:acme:sales"
    body = json.loads(backend.recorded("PATCH", "/v1/channels/sales").content)
    assert body == {"enabled": False}


def test_channel_delete_json_with_yes(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/sales", {"channel_id": "feishu:acme:sales", "enabled": False})
    backend.route("DELETE", "/v1/channels/sales", {"deleted": True, "purged": True})
    result = invoke("--json", "channel", "delete", "sales", "--yes", "--purge")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"deleted": True, "purged": True}
    params = dict(backend.recorded("DELETE", "/v1/channels/sales").url.params)
    assert params == {"purge": "true"}


def test_channel_delete_interactive_confirmation_accepted(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.route("GET", "/v1/channels/sales", {"channel_id": "feishu:acme:sales", "enabled": False})
    backend.route("DELETE", "/v1/channels/sales", {"deleted": True, "channel_id": "feishu:acme:sales"})
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))
    monkeypatch.setattr(cli.typer, "confirm", lambda *args, **kwargs: True)
    result = invoke("channel", "delete", "sales")
    assert result.exit_code == 0, result.stderr
    assert "deleted: feishu:acme:sales" in result.stderr


def test_channel_test_json(backend: FakeBackend) -> None:
    backend.route("POST", "/v1/channels/probe", {"ok": True})
    result = invoke("--json", "channel", "test", "feishu", "--config", "app_id=cli")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_channel_login_timeout_must_be_positive(backend: FakeBackend) -> None:
    result = invoke("channel", "login", "sales", "--timeout", "0")
    assert result.exit_code == 1
    assert "--timeout must be a positive number" in result.stderr


def test_channel_login_posts_timeout_and_hints_validity(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    backend.route(
        "GET",
        "/v1/channels/sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "credentials": {"configured": False},
        },
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {
            "session_id": "session-1",
            "status": "pending",
            "qr_content": "https://example.com/qr",
            "kind": "dingtalk",
            "expires_in_s": 900,
        },
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/login/session-1",
        {"session_id": "session-1", "status": "confirmed"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales",
        {"channel_id": "feishu:acme:sales", "state": "running"},
    )

    result = invoke("channel", "login", "sales")

    assert result.exit_code == 0, result.stderr
    body = json.loads(backend.recorded("POST", "/v1/channels/feishu:acme:sales/login").content)
    assert body == {"timeout_s": 900.0}  # default is 15 minutes
    assert (
        "Scan the QR code with DingTalk (valid for 15 minutes), "
        "or open this link in DingTalk: https://example.com/qr" in result.stderr
    )


def test_channel_login_hint_without_platform_ttl_omits_validity(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    backend.route(
        "GET",
        "/v1/channels/sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "credentials": {"configured": False},
        },
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {
            "session_id": "session-1",
            "status": "pending",
            "qr_content": "https://example.com/qr",
            "kind": "wecom",
        },
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/login/session-1",
        {"session_id": "session-1", "status": "confirmed"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales",
        {"channel_id": "feishu:acme:sales", "state": "running"},
    )

    result = invoke("channel", "login", "sales")

    assert result.exit_code == 0, result.stderr
    assert (
        "Scan the QR code with WeCom, or open this link in WeCom: https://example.com/qr"
        in result.stderr
    )
    assert "valid for" not in result.stderr  # never invent a validity


def _login_routes(backend: FakeBackend, *, final_state: str = "running") -> None:
    """Wire the GET/POST/GET sequence one QR login run performs.

    The resolve call uses the CLI ref (``sales``); the login endpoints use the
    canonical channel_id the daemon returned (``feishu:acme:sales``).
    """
    backend.route(
        "GET",
        "/v1/channels/sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "credentials": {"configured": False},
        },
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {"session_id": "session-1", "status": "pending", "qr_content": "https://example.com/qr"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/login/session-1",
        {"session_id": "session-1", "status": "confirmed"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "state": final_state,
        },
    )


def test_channel_login_confirmed_renders_qr(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    _login_routes(backend)
    result = invoke("channel", "login", "sales")
    assert result.exit_code == 0, result.stderr
    assert "█" in result.stdout  # terminal QR art
    assert "login confirmed: feishu:acme:sales" in result.stderr


def test_channel_login_json_polls_until_confirmed(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    _login_routes(backend)
    result = invoke("--json", "channel", "login", "sales")
    assert result.exit_code == 0, result.stderr
    assert '"status":"confirmed"' in result.stdout


def test_channel_login_times_out_and_cancels(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    backend.route(
        "GET",
        "/v1/channels/sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "credentials": {"configured": False},
        },
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {"session_id": "session-1", "status": "pending", "qr_content": "https://example.com/qr"},
    )
    backend.route(
        "GET",
        "/v1/channels/feishu:acme:sales/login/session-1",
        {"session_id": "session-1", "status": "pending"},
    )
    backend.route(
        "DELETE", "/v1/channels/feishu:acme:sales/login/session-1", {"cancelled": True}
    )
    result = invoke("channel", "login", "sales", "--timeout", "0.001")
    assert result.exit_code == 1
    assert "timed out" in result.stderr
    assert backend.recorded("DELETE", "/v1/channels/feishu:acme:sales/login/session-1") is not None


def test_channel_login_reports_non_pending_status(
    backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    backend.route(
        "GET",
        "/v1/channels/sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "credentials": {"configured": False},
        },
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {"session_id": "session-1", "status": "expired", "message": "session gone", "qr_content": "https://example.com/qr"},
    )
    result = invoke("channel", "login", "sales")
    assert result.exit_code == 1
    assert "login expired: session gone" in result.stderr


def test_emit_login_challenge_encodes_image_url(capsys: pytest.CaptureFixture[str]) -> None:
    # WeChat only returns a page URL (qr_image_url): it must be *encoded* as the
    # QR payload, not downloaded as a bitmap.
    cli._emit_login_challenge({"qr_image_url": "https://example.com/bind"}, "both")
    out = capsys.readouterr().out
    assert "URL: https://example.com/bind" in out
    assert "\u2588" in out
    with pytest.raises(typer.Exit):
        cli._emit_login_challenge({}, "both")


def test_start_wait_polls_ready(backend: FakeBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeController:
        def __init__(self, *, platform: str | None = None, spec: Any = None) -> None:
            pass

        def start(self, *, dry_run: bool = False) -> list[list[str]]:
            return [["systemctl", "--user", "start", "im-gateway"]]

    waits: list[Any] = []
    monkeypatch.setattr(cli, "ServiceController", FakeController)
    monkeypatch.setattr(cli, "_wait_ready", lambda ctx: waits.append(ctx))
    result = invoke("start", "--wait")
    assert result.exit_code == 0, result.stderr
    assert len(waits) == 1


def test_main_entry_point_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["imgw", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0


def test_get_client_builds_a_real_client() -> None:
    client = cli.get_client("http://example.invalid:1", "direct-token")
    try:
        assert str(client.base_url).startswith("http://example.invalid:1")
        assert client.headers["Authorization"] == "Bearer direct-token"
    finally:
        client.close()


def test_fmt_uptime_seconds_only() -> None:
    assert cli._fmt_uptime(45) == "45s"


def test_wait_ready_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_client", lambda url=None, token=None: _ContextClient())
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    calls = {"count": 0}

    def flaky(client: Any, method: str, path: str) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            raise control_plane.ControlPlaneError("daemon starting")
        return {"status": "ready"}

    monkeypatch.setattr(cli.control_plane, "api", flaky)
    cli._wait_ready(context(), timeout_s=5.0)
    assert calls["count"] == 2
    assert "ready: the daemon answered" in capsys.readouterr().out


def test_channel_login_json_reports_error_status(backend: FakeBackend) -> None:
    backend.route(
        "GET",
        "/v1/channels/sales",
        {
            "channel_id": "feishu:acme:sales",
            "login_supported": True,
            "credentials": {"configured": False},
        },
    )
    backend.route(
        "POST",
        "/v1/channels/feishu:acme:sales/login",
        {
            "session_id": "session-1",
            "status": "expired",
            "qr_content": "https://example.com/qr",
        },
    )
    result = invoke("--json", "channel", "login", "sales")
    assert result.exit_code == 1
    assert '"status":"expired"' in result.stdout


def test_subject_ls_empty(backend: FakeBackend) -> None:
    backend.route("GET", "/v1/channels/sales/subjects", {"subjects": []})
    result = invoke("subject", "ls", "sales")
    assert result.exit_code == 0, result.stderr
    assert "(no known subjects yet)" in result.stdout


def test_config_export_write_failure_exits_one(backend: FakeBackend, tmp_path: Path) -> None:
    backend.route(
        "GET",
        "/v1/config/export",
        {"version": 2, "channels": [], "settings": {}},
    )
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    result = invoke("config", "export", "-o", str(directory))
    assert result.exit_code == 1
    assert "cannot write" in result.stderr


def test_config_import_json_output(backend: FakeBackend, tmp_path: Path) -> None:
    backend.route(
        "POST",
        "/v1/config/import",
        {"applied": {"channels": 0, "settings": 0}},
    )
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 2, "channels": [], "settings": {}}),
        encoding="utf-8",
    )
    result = invoke("--json", "config", "import", str(path))
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["applied"]["channels"] == 0


# ---------------------------------------------------------------------------
# cli/service.py
# ---------------------------------------------------------------------------


def test_service_error_from_called_process_error_variants() -> None:
    string_cmd = subprocess.CalledProcessError(3, "systemctl --user start im-gateway", stderr=b"denied\n")
    error = ServiceError.from_called_process_error(string_cmd)
    assert error.command == ["systemctl --user start im-gateway"]
    assert error.stderr == "denied"
    assert "exit code 3" in str(error)

    tuple_cmd = subprocess.CalledProcessError(2, ("launchctl", "load"))
    error = ServiceError.from_called_process_error(tuple_cmd)
    assert error.command == ["launchctl", "load"]
    assert error.stderr == ""

    bytes_cmd = subprocess.CalledProcessError(1, b"nssm", stderr="text-error")
    error = ServiceError.from_called_process_error(bytes_cmd)
    assert error.command == ["nssm"]
    assert error.stderr == "text-error"

    class WeirdCommand:
        def __str__(self) -> str:
            return "weird-command"

    odd = subprocess.CalledProcessError(1, WeirdCommand())
    error = ServiceError.from_called_process_error(odd)
    assert error.command == ["weird-command"]


def test_decode_stream_variants() -> None:
    assert service._decode_stream(None) == ""
    assert service._decode_stream(b"bytes") == "bytes"
    assert service._decode_stream(42) == "42"


def test_default_exec_path_falls_back_to_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        service,
        "sys",
        SimpleNamespace(platform="win32", executable=str(tmp_path / "python.exe")),
    )
    assert service.default_exec_path() == str(tmp_path / "imgw.exe")


def test_default_spec_uses_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "default_exec_path", lambda: "/bin/imgw")
    spec = service.default_spec()
    assert spec.exec_path == "/bin/imgw"
    assert spec.workdir == str(Path.cwd())
    assert spec.user is None


def _spec(tmp_path: Path, *, user: str | None = None) -> ServiceSpec:
    return ServiceSpec(exec_path="imgw", workdir=str(tmp_path), user=user)


def test_default_target_darwin_and_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    user_spec = _spec(tmp_path)
    system_spec = _spec(tmp_path, user="service-user")
    assert service.default_target(platform="darwin", spec=user_spec) == (
        tmp_path / "home" / "Library" / "LaunchAgents"
    )
    assert service.default_target(platform="darwin", spec=system_spec) == Path("/Library/LaunchDaemons")
    with pytest.raises(ServiceError, match="unsupported platform"):
        service.default_target(platform="plan9", spec=user_spec)


def test_default_target_windows_uses_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMGW_DATA_DIR", str(tmp_path / "data"))
    assert service.default_target(platform="win32", spec=_spec(tmp_path)) == tmp_path / "data"


def test_render_command_quotes_empty_argument() -> None:
    assert service.render_command(["", "a b"]) == '"" "a b"'


def test_service_file_path_rejects_unknown_platform(tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="unsupported platform"):
        service.service_file_path(platform="plan9", target=tmp_path)


def test_controller_target_property(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMGW_DATA_DIR", str(tmp_path / "data"))
    controller = service.ServiceController(platform="win32", spec=_spec(tmp_path))
    assert controller.target == tmp_path / "data"


def test_install_reports_write_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken_write(*args: Any, **kwargs: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(service, "write_service_file", broken_write)
    controller = service.ServiceController(platform="linux", spec=_spec(tmp_path))
    with pytest.raises(ServiceError, match="cannot write the service definition"):
        controller.install(target=tmp_path)


def test_uninstall_dry_run_darwin_and_win32(tmp_path: Path) -> None:
    darwin = service.ServiceController(platform="darwin", spec=_spec(tmp_path))
    darwin_commands = darwin.uninstall(target=tmp_path, dry_run=True)
    assert darwin_commands[0][0] == "launchctl"
    assert darwin_commands[0][1] == "unload"

    windows = service.ServiceController(platform="win32", spec=_spec(tmp_path))
    windows_commands = windows.uninstall(target=tmp_path, dry_run=True)
    assert windows_commands == [
        ["nssm", "stop", "im-gateway"],
        ["nssm", "remove", "im-gateway", "confirm"],
    ]


def test_install_rejects_unsupported_platform(tmp_path: Path) -> None:
    controller = service.ServiceController(platform="plan9", spec=_spec(tmp_path))
    with pytest.raises(ServiceError, match="unsupported platform"):
        controller.install(target=tmp_path, dry_run=True)


def test_default_runner_invokes_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    result = service.default_runner(["nssm", "start", "im-gateway"])
    assert result.returncode == 0
    assert recorded == [["nssm", "start", "im-gateway"]]


def test_uninstall_reports_remove_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def locked_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("file is locked")

    monkeypatch.setattr(service.Path, "unlink", locked_unlink)
    controller = service.ServiceController(
        platform="linux", runner=lambda command: subprocess.CompletedProcess(command, 0), spec=_spec(tmp_path)
    )
    with pytest.raises(ServiceError, match="cannot remove"):
        controller.uninstall(target=tmp_path)
