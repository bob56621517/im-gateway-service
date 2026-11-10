"""``imgw`` — the im-gateway command line client.

Two-face design (HANDOVER §5.6, §13.7): ``imgw run`` is the **only** command
that starts a process; every other command talks to the daemon's control-plane
HTTP API. SQLite is never touched directly (the daemon is the only writer).

Hard rule: this module must not import ``im_gateway.gateway.*`` or
``im_gateway.db.*``. The single allowed gateway import is the lazy one inside
:func:`run`.
"""

from __future__ import annotations

import contextlib
import functools
import getpass
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast
from urllib.parse import quote

import httpx
import typer

from im_gateway.cli import client as control_plane
from im_gateway.cli.client import ControlPlaneError
from im_gateway.cli.service import (
    ServiceController,
    ServiceError,
    ServiceSpec,
    default_exec_path,
    default_target,
    render_command,
    render_service_file,
    service_file_path,
)
from im_gateway.core.config import ChannelConfigEntry, ConfigDocument

__all__ = ["app", "main"]

CHANNEL_TABLE_HEADERS = ["CHANNEL_ID", "KIND", "TENANT", "ALIAS", "ENABLED", "STATE", "NAME"]
HISTORY_TABLE_HEADERS = ["TS", "CHANNEL_ID", "SUBJECT_ID", "DIR", "KIND", "TEXT"]
SUBJECT_TABLE_HEADERS = ["SUBJECT_ID", "CHAT_TYPE", "DISPLAY_NAME", "FIRST_SEEN", "LAST_SEEN"]


# ---------------------------------------------------------------------------
# App and global options
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CliOptions:
    """Global options from the root callback, shared via ``ctx.obj``."""

    url: str | None = None
    token: str | None = None
    json_output: bool = False


app = typer.Typer(
    name="imgw",
    help=(
        "im-gateway control CLI.\n\n"
        "Every command except `imgw run` is an HTTP client for the daemon's "
        "control plane; it never touches the database directly."
    ),
    no_args_is_help=True,
    add_completion=False,
)

channel_app = typer.Typer(
    name="channel",
    help="Bind, list, log out and delete channel instances (login / list / logout / delete / test).",
    no_args_is_help=True,
)
subject_app = typer.Typer(
    name="subject",
    help="Inspect the known push targets (subjects) of a channel.",
    no_args_is_help=True,
)
config_app = typer.Typer(
    name="config",
    help="Export, import or locally validate the portable config document.",
    no_args_is_help=True,
)
app.add_typer(channel_app, name="channel")
app.add_typer(subject_app, name="subject")
app.add_typer(config_app, name="config")


@app.callback()
def _root(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            envvar="IMGW_URL",
            help="Control-plane URL: http(s)://host:port or unix:///path/to.sock.",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            envvar="IMGW_TOKEN",
            help="Control-plane token (default: $IMGW_TOKEN_FILE or the daemon token file).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Global options shared by every command."""
    ctx.obj = CliOptions(url=url, token=token, json_output=json_output)


def get_client(url: str | None = None, token: str | None = None) -> httpx.Client:
    """Build a control-plane HTTP client.

    Kept as a module-level seam so tests can monkeypatch
    ``im_gateway.cli.main.get_client`` with an ``httpx.MockTransport`` client
    instead of starting a real daemon.
    """
    return control_plane.build_client(url, token)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _options(ctx: typer.Context) -> CliOptions:
    if isinstance(ctx.obj, CliOptions):
        return ctx.obj
    options = CliOptions()
    ctx.obj = options
    return options


def _want_json(ctx: typer.Context, local: bool = False) -> bool:
    return bool(local or _options(ctx).json_output)


def _print_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _handle_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Print :class:`ControlPlaneError` as ``error: ...`` and exit with code 1."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ControlPlaneError as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

    return wrapper


def _handle_service_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Print :class:`ServiceError` as ``error: ...`` and exit with code 1."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ServiceError as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

    return wrapper


def _ref_path(ref: str) -> str:
    """Percent-encode a channel ref (channel_id or alias) for use in a path."""
    return quote(ref, safe="")


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _fmt_ts(value: Any) -> str:
    if not isinstance(value, int | float):
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (OverflowError, OSError, ValueError):
        return "-"


def _fmt_uptime(value: Any) -> str:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return "-"
    seconds = max(seconds, 0)
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _truncate(value: Any, limit: int = 80) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _content_text(content: Any) -> str:
    """Concatenate the text parts of a history ``content`` list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(part for part in parts if part)


def _channel_row(channel: dict[str, Any]) -> list[str]:
    return [
        str(channel.get("channel_id", "")),
        str(channel.get("kind", "")),
        str(channel.get("tenant_id") or "-"),
        str(channel.get("alias") or "-"),
        "yes" if channel.get("enabled") else "no",
        str(channel.get("state", "")),
        str(channel.get("name") or "-"),
    ]


def _channel_details(channel: dict[str, Any]) -> str:
    capabilities = channel.get("capabilities") or {}
    lines = [
        f"channel_id      {channel.get('channel_id', '-')}",
        f"kind            {channel.get('kind', '-')}",
        f"tenant_id       {channel.get('tenant_id') or '-'}",
        f"alias           {channel.get('alias') or '-'}",
        f"name            {channel.get('name') or '-'}",
        f"enabled         {channel.get('enabled')}",
        f"state           {channel.get('state', '-')}",
        f"last_error      {channel.get('last_error') or '-'}",
        f"created_at      {_fmt_ts(channel.get('created_at'))}",
        f"last_started_at {_fmt_ts(channel.get('last_started_at'))}",
    ]
    if capabilities:
        lines.append(f"capabilities    {json.dumps(capabilities, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def _parse_config_pairs(pairs: list[str] | None) -> dict[str, Any]:
    """Parse repeated ``--config KEY=VALUE`` flags.

    Values are parsed in order bool (``true``/``false``, case-insensitive),
    then int, then left as a string. Milestone 1 supports one-level keys only.
    """
    values: dict[str, Any] = {}
    for pair in pairs or []:
        key, separator, raw = pair.partition("=")
        key = key.strip()
        if not separator or not key:
            raise typer.BadParameter(f"invalid --config {pair!r}: expected KEY=VALUE")
        values[key] = _parse_config_value(raw)
    return values


def _parse_config_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# System service helpers (`imgw install-service` / `start` / `stop` / `restart`)
# ---------------------------------------------------------------------------


def _service_platform() -> str:
    """Platform seam for the service commands (tests monkeypatch it)."""
    return sys.platform


def _current_username() -> str:
    try:
        return getpass.getuser()
    except (ImportError, KeyError, OSError):
        return os.environ.get("USER") or os.environ.get("USERNAME") or "imgw"


def _service_spec(*, workdir: str | None, user_scope: bool) -> ServiceSpec:
    """Build the default spec from the current interpreter / cwd and CLI options."""
    workdir_path = Path(workdir).expanduser() if workdir else Path.cwd()
    return ServiceSpec(
        exec_path=default_exec_path(),
        workdir=str(workdir_path.resolve()),
        user=None if user_scope else _current_username(),
    )


def _emit_service_result(
    ctx: typer.Context,
    commands: list[list[str]],
    *,
    service_file: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> None:
    """Print the command sequence (human ``$ ...`` lines or JSON) and its outcome."""
    if _want_json(ctx, json_output):
        _print_json(
            {
                "dry_run": dry_run,
                "service_file": str(service_file) if service_file is not None else None,
                "commands": commands,
            }
        )
        return
    for command in commands:
        typer.echo(f"$ {render_command(command)}")
    if dry_run:
        typer.echo("(dry run: nothing was written or executed)")
        return
    if service_file is not None:
        typer.echo(f"service definition: {service_file}")
    typer.echo(f"ok: {len(commands)} command(s) executed")


def _wait_ready(ctx: typer.Context, *, timeout_s: float = 30.0) -> None:
    """Poll ``/readyz`` after ``imgw start --wait`` (HANDOVER §13.7)."""
    options = _options(ctx)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with get_client(options.url, options.token) as client:
                control_plane.api(client, "GET", "/readyz")
        except ControlPlaneError as exc:
            if time.monotonic() >= deadline:
                raise ServiceError(
                    f"the daemon did not become ready within {timeout_s:.0f}s: {exc}"
                ) from exc
            time.sleep(0.5)
        else:
            typer.echo("ready: the daemon answered /readyz")
            return


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command()
@_handle_errors
def version(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Show the daemon, octop-gateway and Python versions."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", "/v1/version")
    if _want_json(ctx, json_output):
        _print_json(payload)
        return
    payload = payload if isinstance(payload, dict) else {}
    for label, key in (
        ("service", "service"),
        ("octop-gateway", "octop_gateway"),
        ("python", "python"),
    ):
        value = payload.get(key)
        if value is not None:
            typer.echo(f"{label:16} {value}")


@app.command()
@_handle_errors
def status(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Show daemon status, paths, counters and per-channel state."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", "/v1/status")
    if _want_json(ctx, json_output):
        _print_json(payload)
        return
    payload = payload if isinstance(payload, dict) else {}
    lines = [
        f"service         {payload.get('service_version', '?')} "
        f"(octop-gateway {payload.get('octop_gateway', '?')})",
        f"uptime          {_fmt_uptime(payload.get('uptime_s'))}",
        f"db              {payload.get('db_path', '-')}",
        f"media           {payload.get('media_dir', '-')}",
        f"backends        {payload.get('backends', 0)}",
    ]
    counts = payload.get("counts")
    if isinstance(counts, dict) and counts:
        rendered = " ".join(f"{key}={value}" for key, value in counts.items())
        lines.append(f"counts          {rendered}")
    typer.echo("\n".join(lines))
    channels = payload.get("channels")
    if isinstance(channels, list) and channels:
        rows = [_channel_row(channel) for channel in channels if isinstance(channel, dict)]
        typer.echo("")
        typer.echo(_format_table(CHANNEL_TABLE_HEADERS, rows))


@app.command()
@_handle_errors
def ready(ctx: typer.Context) -> None:
    """Ask the daemon's /readyz; exit 0 when ready, 1 otherwise."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        try:
            payload = control_plane.api(client, "GET", "/readyz")
        except ControlPlaneError as exc:
            typer.secho(f"not ready: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
    state = payload.get("status", "ready") if isinstance(payload, dict) else "ready"
    typer.echo(f"ready: {state}")


@app.command()
@_handle_errors
def send(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
    subject_id: Annotated[str | None, typer.Argument(help="Target subject_id (omit with --all).")] = None,
    text: Annotated[str | None, typer.Argument(help="Message text.")] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Broadcast to every known subject of the channel."),
    ] = False,
) -> None:
    """Push a text message to one subject, or broadcast with --all."""
    options = _options(ctx)
    if all_:
        # `imgw send <ref> --all <text>` leaves `<text>` in subject_id because
        # Typer fills positionals left to right.
        broadcast_text = text if text is not None else subject_id
        if not broadcast_text:
            raise typer.BadParameter("missing TEXT: usage is `imgw send <ref> --all <text>`")
        with get_client(options.url, options.token) as client:
            payload = control_plane.api(
                client,
                "POST",
                "/v1/messages/broadcast",
                json_body={"channel_id": ref, "text": broadcast_text},
            )
        if _want_json(ctx):
            _print_json(payload)
            return
        payload = payload if isinstance(payload, dict) else {}
        typer.echo(f"broadcast: sent={payload.get('sent', 0)} failed={payload.get('failed', 0)}")
        return

    if subject_id is None or text is None:
        raise typer.BadParameter(
            "usage is `imgw send <ref> <subject_id> <text>` or `imgw send <ref> --all <text>`"
        )
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(
            client,
            "POST",
            "/v1/messages",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json_body={"channel_id": ref, "subject_id": subject_id, "text": text},
        )
    if _want_json(ctx):
        _print_json(payload)
        return
    typer.echo(f"sent to {ref}/{subject_id}")


@app.command()
@_handle_errors
def history(
    ctx: typer.Context,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Filter by channel_id or alias."),
    ] = None,
    subject: Annotated[str | None, typer.Option("--subject", help="Filter by subject_id.")] = None,
    since: Annotated[
        float | None,
        typer.Option("--since", help="Only messages at/after this epoch timestamp."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum number of messages.")] = 50,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Query the daemon's message history (read-only)."""
    options = _options(ctx)
    params: dict[str, Any] = {"limit": limit}
    if channel:
        params["channel_id"] = channel
    if subject:
        params["subject_id"] = subject
    if since is not None:
        params["since"] = since
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", "/v1/history", params=params)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    messages = messages or []
    if _want_json(ctx, json_output):
        _print_json({"messages": messages})
        return
    if not messages:
        typer.echo("(no messages)")
        return
    rows = [
        [
            _fmt_ts(message.get("ts")),
            str(message.get("channel_id", "")),
            str(message.get("subject_id", "")),
            str(message.get("direction", "")),
            str(message.get("kind", "")),
            _truncate(_content_text(message.get("content"))),
        ]
        for message in messages
        if isinstance(message, dict)
    ]
    typer.echo(_format_table(HISTORY_TABLE_HEADERS, rows))


@app.command()
@_handle_errors
def events(
    ctx: typer.Context,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Only events for this channel_id or alias."),
    ] = None,
) -> None:
    """Tail the daemon's SSE event stream until Ctrl-C."""
    options = _options(ctx)
    params: dict[str, Any] | None = {"channel_id": channel} if channel else None
    try:
        with (
            get_client(options.url, options.token) as client,
            client.stream("GET", "/v1/events", params=params) as response,
        ):
            if not response.is_success:
                response.read()
                control_plane.decode_response(response)
            for line in response.iter_lines():
                payload = _sse_data(line)
                if payload is not None:
                    typer.echo(payload)
    except httpx.TransportError as exc:
        raise ControlPlaneError(
            f"cannot reach the im-gateway daemon: {exc}. Start it with `imgw run`."
        ) from exc
    except KeyboardInterrupt:
        raise typer.Exit(0) from None


def _sse_data(line: str) -> str | None:
    """Return the payload of an SSE ``data:`` line, or ``None`` for noise."""
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    return data or None


@app.command()
@_handle_errors
def doctor(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Run control-plane self-checks; exit 1 if any check fails."""
    options = _options(ctx)
    checks: list[dict[str, Any]] = []
    with get_client(options.url, options.token) as client:
        for name, method, path in (
            ("healthz", "GET", "/healthz"),
            ("readyz", "GET", "/readyz"),
            ("version", "GET", "/v1/version"),
            ("status", "GET", "/v1/status"),
        ):
            try:
                payload = control_plane.api(client, method, path)
            except ControlPlaneError as exc:
                checks.append({"check": name, "ok": False, "detail": str(exc)})
            else:
                checks.append({"check": name, "ok": True, "detail": _doctor_detail(name, payload)})
    ok = all(check["ok"] for check in checks)
    if _want_json(ctx, json_output):
        _print_json({"ok": ok, "checks": checks})
    else:
        for check in checks:
            marker = "ok  " if check["ok"] else "FAIL"
            typer.echo(f"[{marker}] {check['check']}: {check['detail']}")
        typer.echo("all checks passed" if ok else "doctor found problems")
    if not ok:
        raise typer.Exit(1)


def _doctor_detail(name: str, payload: Any) -> str:
    payload = payload if isinstance(payload, dict) else {}
    if name == "status":
        counts = payload.get("counts")
        channels = counts.get("channels") if isinstance(counts, dict) else None
        return f"service={payload.get('service_version', '?')} channels={channels}"
    if name == "version":
        return f"service={payload.get('service', '?')} octop-gateway={payload.get('octop_gateway', '?')}"
    return str(payload.get("status", "ok"))


@app.command()
def run(
    host: Annotated[str, typer.Option("--host", help="Control-plane listen host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Control-plane listen port.")] = 8765,
) -> None:
    """Start the gateway daemon in the foreground (the production entry point)."""
    # TODO(milestone-2): also expose --db / --listen once the daemon accepts them.
    _start_daemon(host=host, port=port)


@app.command("run-dev")
def run_dev(
    host: Annotated[str, typer.Option("--host", help="Control-plane listen host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Control-plane listen port.")] = 8765,
    dev_backend_port: Annotated[
        int,
        typer.Option("--dev-backend-port", help="Loopback port for the in-process echo backend."),
    ] = 9900,
) -> None:
    """Start the daemon in dev mode: every tenant is routed to the echo backend.

    The echo backend (``im_gateway.dev.backend``) replies to any inbound message
    with a fixed text that lists channel / subject / turn / idempotency / text,
    so gateway–channel connectivity can be verified without a real webhook.
    """
    _start_daemon(host=host, port=port, dev_backend=True, dev_backend_port=dev_backend_port)


def _start_daemon(*, host: str, port: int, **extra: Any) -> None:
    # Deliberate lazy import: the only place the CLI is allowed to touch the
    # gateway package, so plain HTTP commands never load the channel SDKs.
    try:
        from im_gateway.gateway.app import run_daemon
    except ImportError as exc:  # pragma: no cover - only when the daemon side is absent
        typer.secho(f"error: the gateway runtime is not available: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    run_daemon(host=host, port=port, **extra)


@app.command("install-service")
@_handle_service_errors
def install_service(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the commands without writing files or running them."),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help="Install a user-scoped service (default, no root) or a system-wide one (needs root).",
        ),
    ] = True,
    workdir: Annotated[
        str | None,
        typer.Option("--workdir", help="Working directory of the daemon (default: the current directory)."),
    ] = None,
    print_only: Annotated[
        bool,
        typer.Option(
            "--print-only",
            help="Print the generated definition without writing or executing anything.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Generate and install the platform service definition (systemd / launchd / NSSM)."""
    platform = _service_platform()
    spec = _service_spec(workdir=workdir, user_scope=user_scope)
    target = default_target(platform=platform, spec=spec)

    if print_only:
        typer.echo(render_service_file(spec, platform=platform, target=target), nl=False)
        return

    commands = ServiceController(platform=platform, spec=spec).install(target=target, dry_run=dry_run)
    service_file = None if dry_run else service_file_path(platform=platform, target=target)
    _emit_service_result(
        ctx,
        commands,
        service_file=service_file,
        dry_run=dry_run,
        json_output=json_output,
    )


@app.command("uninstall-service")
@_handle_service_errors
def uninstall_service(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the commands without executing them."),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help="Manage the user-scoped service (default) or the system-wide one.",
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Stop and remove the platform service definition."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    target = default_target(platform=platform, spec=spec)
    commands = ServiceController(platform=platform, spec=spec).uninstall(target=target, dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)


@app.command("start")
@_handle_service_errors
def start(
    ctx: typer.Context,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Poll /readyz until the daemon is ready."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the commands without executing them."),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help="Manage the user-scoped service (default) or the system-wide one.",
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Start the system service."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    commands = ServiceController(platform=platform, spec=spec).start(dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)
    if wait and not dry_run:
        _wait_ready(ctx)


@app.command("stop")
@_handle_service_errors
def stop(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the commands without executing them."),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help="Manage the user-scoped service (default) or the system-wide one.",
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Stop the system service."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    commands = ServiceController(platform=platform, spec=spec).stop(dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)


@app.command("restart")
@_handle_service_errors
def restart(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the commands without executing them."),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help="Manage the user-scoped service (default) or the system-wide one.",
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Restart the system service."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    commands = ServiceController(platform=platform, spec=spec).restart(dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)


# ---------------------------------------------------------------------------
# `imgw channel ...` — bind / unbind / list / delete / probe
# ---------------------------------------------------------------------------
#
# Output contract (pipe-safe): data goes to **stdout**, progress goes to
# **stderr**. `channel list` is TSV by default (one channel per line, no
# header); `--header` adds a human header and `--json` switches to structured
# output. Secret values never leave the daemon: the `config` view is masked.

_LOGIN_FORMATS = ("both", "url", "qr", "json")


def _stderr(text: str) -> None:
    """Progress/notice output; never pollutes a data pipe."""
    typer.secho(text, err=True)


def _fail(message: str) -> NoReturn:
    _stderr(f"error: {message}")
    raise typer.Exit(1)


def _compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _credentials_label(channel: dict[str, Any]) -> str:
    credentials = channel.get("credentials")
    configured = credentials.get("configured") if isinstance(credentials, dict) else None
    if configured is True:
        return "ok"
    if configured is False:
        return "missing"
    return "n/a"


def _channel_or_none(client: httpx.Client, ref: str) -> dict[str, Any] | None:
    try:
        payload = control_plane.api(client, "GET", f"/v1/channels/{_ref_path(ref)}")
    except ControlPlaneError as exc:
        if exc.status_code == 404:
            return None
        raise
    return cast("dict[str, Any]", payload)


def _get_channel(client: httpx.Client, ref: str) -> dict[str, Any]:
    payload = control_plane.api(client, "GET", f"/v1/channels/{_ref_path(ref)}")
    return cast("dict[str, Any]", payload)


def _build_login_config(
    kind: str,
    *,
    app_id: str,
    app_secret: str,
    bot_id: str,
    secret: str,
    app_key: str,
    token: str,
    account_id: str,
    base_url: str,
    config_pairs: list[str] | None,
    secret_stdin: bool,
) -> dict[str, Any]:
    """Merge credential flags + repeated ``--config`` pairs into one config dict."""
    values = _parse_config_pairs(config_pairs)
    stdin_secret = ""
    if secret_stdin:
        stdin_secret = sys.stdin.readline().strip()
        if not stdin_secret:
            _fail("--secret-stdin：stdin 上没读到 secret")
    if app_id:
        values["app_id"] = app_id
    if app_secret:
        values["app_secret"] = app_secret
    if bot_id:
        values["bot_id"] = bot_id
    if secret:
        values["secret"] = secret
    if app_key:
        values["app_key"] = app_key
    if token:
        values["token"] = token
    if base_url:
        values["base_url"] = base_url
    if account_id:
        account: dict[str, Any] = {"account_id": account_id}
        if token:
            account["token"] = token
        if base_url:
            account["base_url"] = base_url
        values["accounts"] = [account]
    if stdin_secret:
        if kind == "wecom":
            values["secret"] = stdin_secret
        elif kind in {"feishu", "qq", "dingtalk"}:
            values["app_secret"] = stdin_secret
        elif kind == "weixin":
            accounts = values.get("accounts")
            if isinstance(accounts, list):
                for account in accounts:
                    if isinstance(account, dict):
                        account.setdefault("token", stdin_secret)
            else:
                values["token"] = stdin_secret
        else:
            values["secret"] = stdin_secret
    return values


def _login_qr_text(challenge: dict[str, Any]) -> str:
    """Return the text to encode as a QR code.

    ``qr_content`` is text for platforms that hand back a payload; WeChat's
    iLink flow only returns ``qr_image_url`` — a **page URL that must itself be
    encoded** (opening it in the WeChat app performs the binding), not a bitmap.
    """
    return str(challenge.get("qr_content") or challenge.get("qr_image_url") or "")


def _write_login_qr_png(challenge: dict[str, Any], path: str) -> None:
    from im_gateway.cli.qr import render_qr_bytes

    text = _login_qr_text(challenge)
    if not text:
        _fail("后端没有返回二维码内容，无法写出 PNG")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_qr_bytes(text))
    _stderr(f"QR PNG: {target}")


def _stdout_can_encode(text: str, encoding: str | None = None) -> bool:
    """Whether ``sys.stdout`` can encode *text* (Windows consoles may be GBK)."""
    encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _emit_terminal_qr(content: str) -> None:
    from im_gateway.cli.qr import render_qr

    art = render_qr(content)
    if _stdout_can_encode(art):
        typer.echo(art)
        return
    # A GBK/cp936 console cannot print half-block characters: keep the URL
    # usable and point at the PNG / URL alternatives instead of crashing.
    _stderr("(终端编码不支持二维码字符；已改为只输出 URL，可用 --qr-png 生成图片)")
    typer.echo(content)


def _emit_login_challenge(challenge: dict[str, Any], output_format: str) -> None:
    text = _login_qr_text(challenge)
    if not text:
        _fail("后端没有返回二维码内容")
    if output_format == "json":
        typer.echo(_compact_json({"event": "challenge", **challenge}))
        return
    if output_format == "url":
        typer.echo(text)
        return
    if output_format == "qr":
        _emit_terminal_qr(text)
        return
    typer.echo(f"URL: {text}")
    _emit_terminal_qr(text)


def _qr_login(
    client: httpx.Client,
    ref: str,
    *,
    output_format: str,
    qr_png: str,
    timeout: float,
    interval: float,
    no_wait: bool,
    session_id: str,
) -> dict[str, Any]:
    """Run one QR login session (or resume ``session_id``) until terminal."""
    path = f"/v1/channels/{_ref_path(ref)}/login"
    challenge: dict[str, Any]
    if session_id:
        challenge = {"session_id": session_id, "channel_id": ref, "status": "pending"}
        if output_format == "json":
            typer.echo(_compact_json({"event": "challenge", **challenge}))
    else:
        challenge = control_plane.api(client, "POST", path)
        sid = str(challenge.get("session_id") or "")
        if not sid:
            _fail("daemon 未返回 login session_id")
        if qr_png:
            _write_login_qr_png(challenge, qr_png)
        _emit_login_challenge(challenge, output_format)
    sid = str(challenge.get("session_id") or "")
    if not sid:
        _fail("daemon 未返回 login session_id")
    if no_wait:
        _stderr(f"session: {sid} (resume with --session-id {sid})")
        return challenge
    deadline = time.monotonic() + max(0.1, timeout)
    status = str(challenge.get("status") or "pending")
    result: dict[str, Any] = challenge
    while status in {"pending", "scanned"} and time.monotonic() < deadline:
        time.sleep(max(0.1, interval))
        result = control_plane.api(client, "GET", f"{path}/{_ref_path(sid)}")
        next_status = str(result.get("status") or status)
        if next_status != status and output_format != "json":
            _stderr(f"status: {next_status}")
        status = next_status
    if status in {"confirmed", "success"}:
        if output_format == "json":
            typer.echo(_compact_json({"event": "result", **result}))
        _stderr(f"login confirmed: {ref}")
        message = str(result.get("message") or "")
        if message:
            _stderr(f"warning: {message}")
        return result
    if status in {"pending", "scanned"}:
        with contextlib.suppress(ControlPlaneError):
            control_plane.api(client, "DELETE", f"{path}/{_ref_path(sid)}")
        _fail(f"login timed out after {timeout:g}s")
    if output_format == "json":
        typer.echo(_compact_json({"event": "result", **result}))
    message = str(result.get("message") or "")
    _fail(f"login {status}" + (f": {message}" if message else ""))


def _report_logged_in(channel: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        typer.echo(_compact_json({"event": "result", **channel}))
        return
    _stderr(f"logged in: {channel.get('channel_id')} ({channel.get('state')})")


def _flatten_config(config: dict[str, Any], prefix: str = "config") -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for key, value in config.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            lines.extend(_flatten_config(value, path))
        elif isinstance(value, list):
            lines.append((path, json.dumps(value, ensure_ascii=False)))
        else:
            lines.append((path, str(value)))
    return lines


def _channel_detail_lines(channel: dict[str, Any]) -> list[tuple[str, str]]:
    credentials = channel.get("credentials")
    credentials = credentials if isinstance(credentials, dict) else {}
    missing = credentials.get("missing")
    missing_text = ",".join(str(item) for item in missing) if isinstance(missing, list) else "-"
    lines: list[tuple[str, str]] = [
        ("channel_id", str(channel.get("channel_id") or "-")),
        ("kind", str(channel.get("kind") or "-")),
        ("tenant_id", str(channel.get("tenant_id") or "-")),
        ("alias", str(channel.get("alias") or "-")),
        ("name", str(channel.get("name") or "-")),
        ("enabled", str(bool(channel.get("enabled")))),
        ("state", str(channel.get("state") or "-")),
        ("last_error", str(channel.get("last_error") or "-")),
        ("login_supported", str(bool(channel.get("login_supported")))),
        ("credentials", _credentials_label(channel)),
        ("credentials.missing", missing_text or "-"),
        ("created_at", _fmt_ts(channel.get("created_at"))),
        ("last_started_at", _fmt_ts(channel.get("last_started_at"))),
    ]
    config = channel.get("config")
    if isinstance(config, dict):
        lines.extend(_flatten_config(config))
    capabilities = channel.get("capabilities")
    if isinstance(capabilities, dict) and capabilities:
        lines.append(("capabilities", json.dumps(capabilities, ensure_ascii=False, sort_keys=True)))
    return lines


def _channel_tsv_row(channel: dict[str, Any]) -> list[str]:
    return [
        str(channel.get("channel_id") or ""),
        str(channel.get("kind") or ""),
        str(channel.get("state") or ""),
        _credentials_label(channel),
        str(channel.get("alias") or "-"),
    ]


@channel_app.command("login")
@_handle_errors
def channel_login(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "Existing channel_id/alias (re-login) or a channel kind "
                "(feishu/wecom/dingtalk/qq/weixin) to create a new channel."
            )
        ),
    ],
    channel_id: Annotated[
        str,
        typer.Option("--id", help="Immutable channel_id for a new channel (default: <kind>:<name>)."),
    ] = "",
    name: Annotated[
        str,
        typer.Option("--name", help="Instance name for a new channel (also used as the CLI alias)."),
    ] = "",
    tenant: Annotated[
        str | None,
        typer.Option("--tenant", help="Tenant id (backend routing key) for a new channel."),
    ] = None,
    config: Annotated[
        list[str] | None,
        typer.Option("--config", help="Channel config as KEY=VALUE; repeatable."),
    ] = None,
    app_id: Annotated[str, typer.Option("--app-id", help="feishu/qq: app_id.")] = "",
    app_secret: Annotated[str, typer.Option("--app-secret", help="feishu/qq/dingtalk: app_secret.")] = "",
    bot_id: Annotated[str, typer.Option("--bot-id", help="wecom: bot_id.")] = "",
    secret: Annotated[str, typer.Option("--secret", help="wecom: bot secret.")] = "",
    app_key: Annotated[str, typer.Option("--app-key", help="dingtalk: app_key (client_id).")] = "",
    token: Annotated[str, typer.Option("--token", help="qq/weixin: bot token.")] = "",
    account_id: Annotated[str, typer.Option("--account-id", help="weixin: account id.")] = "",
    base_url: Annotated[str, typer.Option("--base-url", help="weixin: API base URL.")] = "",
    secret_stdin: Annotated[
        bool,
        typer.Option("--secret-stdin", help="Read the secret from stdin instead of argv."),
    ] = False,
    rescan: Annotated[
        bool,
        typer.Option("--rescan", help="Force a fresh QR scan even when credentials exist."),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Probe credentials before registering a new channel."),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="QR output: both (default) | url | qr | json."),
    ] = "both",
    json_output: Annotated[bool, typer.Option("--json", help="Alias for --format json.")] = False,
    qr_png: Annotated[
        str,
        typer.Option("--qr-png", help="Also write the QR code to this PNG path."),
    ] = "",
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Overall wait for confirmation (seconds)."),
    ] = 300.0,
    interval: Annotated[float, typer.Option("--interval", help="Poll interval (seconds).")] = 2.0,
    no_wait: Annotated[
        bool,
        typer.Option("--no-wait", help="Print the challenge and exit; resume with --session-id."),
    ] = False,
    session_id: Annotated[
        str,
        typer.Option("--session-id", help="Resume an existing login session (same channel ref)."),
    ] = "",
) -> None:
    """Bind or re-bind a channel: QR scan, silent credentials, or reconnect.

    With no credential flags an existing bound channel is simply re-enabled;
    a channel without credentials starts the platform QR flow.  Providing
    credential flags registers a new channel or overwrites the existing
    credentials without scanning.
    """
    options = _options(ctx)
    if _want_json(ctx, json_output):
        output_format = "json"
    if output_format not in _LOGIN_FORMATS:
        _fail(f"不支持的 --format: {output_format!r}（可选 {'/'.join(_LOGIN_FORMATS)}）")
    if timeout <= 0:
        _fail("--timeout 必须为正数")
    values = _build_login_config(
        target,
        app_id=app_id,
        app_secret=app_secret,
        bot_id=bot_id,
        secret=secret,
        app_key=app_key,
        token=token,
        account_id=account_id,
        base_url=base_url,
        config_pairs=config,
        secret_stdin=secret_stdin,
    )
    with get_client(options.url, options.token) as client:
        existing = _channel_or_none(client, target)
        if existing is None:
            if session_id:
                _fail("--session-id 只能用于已存在的通道")
            alias = name or None
            new_id = channel_id or (f"{target}:{alias}" if alias else f"{target}:{uuid.uuid4().hex[:6]}")
            entry = ChannelConfigEntry(
                channel_id=new_id,
                kind=target,
                tenant_id=tenant,
                alias=alias,
                name=name,
                enabled=bool(values),
                config=values,
            )
            if values and check:
                control_plane.api(
                    client,
                    "POST",
                    "/v1/channels/probe",
                    json_body=entry.model_dump(mode="json"),
                )
            created = control_plane.api(
                client,
                "POST",
                "/v1/channels",
                json_body=entry.model_dump(mode="json"),
            )
            if values:
                _report_logged_in(created, output_format)
                return
            _stderr(f"created {new_id} (disabled until the QR login completes)")
            _qr_login(
                client,
                new_id,
                output_format=output_format,
                qr_png=qr_png,
                timeout=timeout,
                interval=interval,
                no_wait=no_wait,
                session_id="",
            )
            if no_wait:
                return
            _report_logged_in(_get_channel(client, new_id), output_format)
            return

        ref = str(existing.get("channel_id") or target)
        if values:
            updated = control_plane.api(
                client,
                "PATCH",
                f"/v1/channels/{_ref_path(ref)}",
                json_body={"config": values, "enabled": True},
            )
            _report_logged_in(updated, output_format)
            return
        credentials = existing.get("credentials")
        configured = credentials.get("configured") if isinstance(credentials, dict) else None
        if rescan or configured is False:
            if not existing.get("login_supported"):
                _fail(
                    f"通道 {ref!r}（{existing.get('kind')}）不支持扫码登录；"
                    "请用凭据参数（--app-id/--app-secret 等）直接登录"
                )
            _qr_login(
                client,
                ref,
                output_format=output_format,
                qr_png=qr_png,
                timeout=timeout,
                interval=interval,
                no_wait=no_wait,
                session_id=session_id,
            )
            if no_wait:
                return
            _report_logged_in(_get_channel(client, ref), output_format)
            return
        updated = control_plane.api(
            client,
            "PATCH",
            f"/v1/channels/{_ref_path(ref)}",
            json_body={"enabled": True},
        )
        _report_logged_in(updated, output_format)


@channel_app.command("logout")
@_handle_errors
def channel_logout(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Log a channel out (disable it), keeping credentials, data and history."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(
            client,
            "PATCH",
            f"/v1/channels/{_ref_path(ref)}",
            json_body={"enabled": False},
        )
    if _want_json(ctx, json_output):
        _print_json(payload)
        return
    _stderr(
        f"logged out: {payload.get('channel_id')} "
        "(disabled; credentials and history kept)"
    )


@channel_app.command("list")
@_handle_errors
def channel_list(
    ctx: typer.Context,
    ref: Annotated[
        str | None,
        typer.Argument(help="Optional channel_id/alias to show one channel in detail."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of TSV.")] = False,
    header: Annotated[
        bool,
        typer.Option(
            "--header/--no-header",
            help="Print the TSV header line (default: on; use --no-header for bare rows).",
        ),
    ] = True,
) -> None:
    """List channels (TSV with a header by default) or show one channel in detail.

    TSV columns: channel_id, kind, state, creds, alias.  The detail view
    includes the masked ``config`` (secret values are always ``***``).
    """
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        if ref:
            channel = _get_channel(client, ref)
            channels = [channel]
        else:
            payload = control_plane.api(client, "GET", "/v1/channels")
            channels = [
                item
                for item in (payload.get("channels") or [])
                if isinstance(item, dict)
            ]
    if _want_json(ctx, json_output):
        _print_json(channels[0] if ref else {"channels": channels})
        return
    if ref:
        for key, value in _channel_detail_lines(channels[0]):
            typer.echo(f"{key}\t{value}")
        return
    if channels and header:
        typer.echo("\t".join(["channel_id", "kind", "state", "creds", "alias"]))
    for channel in channels:
        typer.echo("\t".join(_channel_tsv_row(channel)))
    if not channels:
        _stderr("(no channels; bind one with `imgw channel login <kind>`)")


@channel_app.command("delete")
@_handle_errors
def channel_delete(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
    purge: Annotated[
        bool,
        typer.Option("--purge", help="Also delete the channel's subjects and messages."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the interactive confirmation."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Delete a channel row; the channel must be logged out first."""
    if not yes:
        if not sys.stdin.isatty():
            _fail("refusing to delete without confirmation; re-run with --yes (stdin is not a TTY)")
        typer.confirm(f"delete channel {ref!r}?", abort=True)
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        current = _channel_or_none(client, ref)
        if current is None:
            _fail(f"unknown channel: {ref!r}")
        if current.get("enabled"):
            _fail(
                f"channel {current.get('channel_id')!r} is still logged in; "
                f"run `imgw channel logout {current.get('channel_id')}` first"
            )
        payload = control_plane.api(
            client,
            "DELETE",
            f"/v1/channels/{_ref_path(ref)}",
            params={"purge": "true" if purge else "false"},
        )
    if _want_json(ctx, json_output):
        _print_json(payload)
        return
    detail = "subjects/messages purged" if purge else "subjects/messages kept; use --purge to remove"
    _stderr(f"deleted: {payload.get('channel_id')} ({detail})")


@channel_app.command("test")
@_handle_errors
def channel_test(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Channel kind to probe, e.g. feishu.")],
    config: Annotated[
        list[str] | None,
        typer.Option("--config", help="Channel config as KEY=VALUE; repeatable."),
    ] = None,
) -> None:
    """Probe credentials without registering a channel."""
    entry = ChannelConfigEntry(
        channel_id=f"{kind}:probe",
        kind=kind,
        config=_parse_config_pairs(config),
    )
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(
            client,
            "POST",
            "/v1/channels/probe",
            json_body=entry.model_dump(mode="json"),
        )
    if _want_json(ctx):
        _print_json(payload)
        return
    typer.echo(f"probe ok: {kind}")


# ---------------------------------------------------------------------------
# `imgw subject ...`
# ---------------------------------------------------------------------------


@subject_app.command("ls")
@_handle_errors
def subject_ls(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """List the known push targets (subjects) of a channel."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", f"/v1/channels/{_ref_path(ref)}/subjects")
    subjects = payload.get("subjects") if isinstance(payload, dict) else None
    subjects = [subject for subject in (subjects or []) if isinstance(subject, dict)]
    if _want_json(ctx, json_output):
        _print_json({"subjects": subjects})
        return
    if not subjects:
        typer.echo("(no known subjects yet)")
        return
    rows = [
        [
            str(subject.get("subject_id", "")),
            str(subject.get("chat_type", "")),
            str(subject.get("display_name", "")),
            _fmt_ts(subject.get("first_seen")),
            _fmt_ts(subject.get("last_seen")),
        ]
        for subject in subjects
    ]
    typer.echo(_format_table(SUBJECT_TABLE_HEADERS, rows))


# ---------------------------------------------------------------------------
# `imgw config ...`
# ---------------------------------------------------------------------------


@config_app.command("export")
@_handle_errors
def config_export(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the document to this file instead of stdout."),
    ] = None,
    with_secrets: Annotated[
        bool,
        typer.Option("--with-secrets", help="Include literal secrets instead of masking them."),
    ] = False,
) -> None:
    """Export the portable config document (secrets masked by default)."""
    options = _options(ctx)
    params = {"with_secrets": "true"} if with_secrets else None
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", "/v1/config/export", params=params)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if output is None:
        typer.echo(rendered, nl=False)
        return
    try:
        output.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        typer.secho(f"error: cannot write {output}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    typer.echo(f"exported config to {output}")


@config_app.command("import")
@_handle_errors
def config_import(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Config document to import."),
    ],
) -> None:
    """Validate and import a config document in one daemon-side transaction."""
    try:
        document = ConfigDocument.model_validate_json(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.secho(f"error: invalid config document {file}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(
            client,
            "POST",
            "/v1/config/import",
            json_body=document.model_dump(mode="json"),
        )
    if _want_json(ctx):
        _print_json(payload)
        return
    applied = payload.get("applied") if isinstance(payload, dict) else None
    applied = applied if isinstance(applied, dict) else {}
    typer.echo(
        "applied: "
        f"channels={applied.get('channels', 0)} "
        f"backends={applied.get('backends', 0)} "
        f"settings={applied.get('settings', 0)}"
    )


@config_app.command("validate")
def config_validate(
    file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Config document to validate."),
    ],
) -> None:
    """Validate a config document locally (no daemon required)."""
    try:
        document = ConfigDocument.model_validate_json(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.secho(f"error: invalid config document {file}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"config OK: {len(document.channels)} channel(s), "
        f"{len(document.backends)} backend(s), {len(document.settings)} setting(s)"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point (``imgw``)."""
    app()
