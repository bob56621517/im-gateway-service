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
from typing import Annotated, Any
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
    help="Manage channel instances (add / show / rm / enable / disable / test).",
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
    """Start the gateway daemon in the foreground (the only process-starting command)."""
    # TODO(milestone-2): also expose --db / --listen once the daemon accepts them.
    # Deliberate lazy import: the only place the CLI is allowed to touch the
    # gateway package, so plain HTTP commands never load the channel SDKs.
    try:
        from im_gateway.gateway.app import run_daemon
    except ImportError as exc:  # pragma: no cover - only when the daemon side is absent
        typer.secho(f"error: the gateway runtime is not available: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    run_daemon(host=host, port=port)


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
# `imgw channel ...`
# ---------------------------------------------------------------------------


@channel_app.command("ls")
@_handle_errors
def channel_ls(
    ctx: typer.Context,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Also list disabled channels."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """List channel instances."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", "/v1/channels")
    channels = payload.get("channels") if isinstance(payload, dict) else None
    channels = [channel for channel in (channels or []) if isinstance(channel, dict)]
    if not all_:
        channels = [channel for channel in channels if channel.get("enabled", True)]
    if _want_json(ctx, json_output):
        _print_json({"channels": channels})
        return
    if not channels:
        typer.echo("(no channels; add one with `imgw channel add --help`)")
        return
    typer.echo(_format_table(CHANNEL_TABLE_HEADERS, [_channel_row(channel) for channel in channels]))


@channel_app.command("show")
@_handle_errors
def channel_show(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Show one channel, including its state and capabilities."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(client, "GET", f"/v1/channels/{_ref_path(ref)}")
    if _want_json(ctx, json_output):
        _print_json(payload)
        return
    typer.echo(_channel_details(payload if isinstance(payload, dict) else {}))


@channel_app.command("add")
@_handle_errors
def channel_add(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Channel kind, e.g. feishu / wecom / telegram.")],
    channel_id: Annotated[
        str,
        typer.Option("--id", help="Immutable routing id, e.g. feishu:acme:sales."),
    ],
    tenant: Annotated[str | None, typer.Option("--tenant", help="Tenant id (backend routing key).")] = None,
    alias: Annotated[str | None, typer.Option("--alias", help="Stable short name for the CLI.")] = None,
    name: Annotated[str, typer.Option("--name", help="Display name.")] = "",
    config: Annotated[
        list[str] | None,
        typer.Option("--config", help="Channel config as KEY=VALUE; repeatable."),
    ] = None,
    disabled: Annotated[
        bool,
        typer.Option("--disabled", help="Create the channel in the disabled state."),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Probe the credentials before registering the channel."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of human-readable output."),
    ] = False,
) -> None:
    """Register a new channel instance."""
    entry = ChannelConfigEntry(
        channel_id=channel_id,
        kind=kind,
        tenant_id=tenant,
        alias=alias,
        name=name,
        enabled=not disabled,
        config=_parse_config_pairs(config),
    )
    body = entry.model_dump(mode="json")
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        if check:
            control_plane.api(client, "POST", "/v1/channels/probe", json_body=body)
            typer.echo(f"probe ok: {kind}")
        payload = control_plane.api(client, "POST", "/v1/channels", json_body=body)
    if _want_json(ctx, json_output):
        _print_json(payload)
        return
    payload = payload if isinstance(payload, dict) else {}
    typer.echo(f"added channel {payload.get('channel_id', channel_id)}")


@channel_app.command("rm")
@_handle_errors
def channel_rm(
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
) -> None:
    """Delete a channel instance."""
    if not yes:
        if not sys.stdin.isatty():
            typer.secho(
                "error: refusing to delete without confirmation; re-run with --yes "
                "(stdin is not a TTY)",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        typer.confirm(f"delete channel {ref!r}?", abort=True)
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(
            client,
            "DELETE",
            f"/v1/channels/{_ref_path(ref)}",
            params={"purge": "true" if purge else "false"},
        )
    if _want_json(ctx):
        _print_json(payload)
        return
    suffix = " (subjects and messages purged)" if purge else ""
    typer.echo(f"deleted channel {ref}{suffix}")


def _set_channel_enabled(ctx: typer.Context, ref: str, enabled: bool) -> None:
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        payload = control_plane.api(
            client,
            "PATCH",
            f"/v1/channels/{_ref_path(ref)}",
            json_body={"enabled": enabled},
        )
    if _want_json(ctx):
        _print_json(payload)
        return
    typer.echo(f"channel {ref} {'enabled' if enabled else 'disabled'}")


@channel_app.command("enable")
@_handle_errors
def channel_enable(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
) -> None:
    """Enable a channel, keeping its config and data."""
    _set_channel_enabled(ctx, ref, True)


@channel_app.command("disable")
@_handle_errors
def channel_disable(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
) -> None:
    """Disable a channel, keeping its config and data."""
    _set_channel_enabled(ctx, ref, False)


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


@channel_app.command("login")
@_handle_errors
def channel_login(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="channel_id or alias.")],
    timeout: Annotated[
        float, typer.Option("--timeout", help="Overall wait for confirmation (seconds).")
    ] = 300.0,
    interval: Annotated[float, typer.Option("--interval", help="Poll interval (seconds).")] = 2.0,
) -> None:
    """QR login for QQ / Weixin channels; renders the QR in the terminal."""
    options = _options(ctx)
    if timeout <= 0:
        typer.secho("error: --timeout must be positive", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    wait_s = max(0.1, interval)
    deadline = time.monotonic() + timeout
    json_mode = _want_json(ctx)
    path = f"/v1/channels/{_ref_path(ref)}/login"

    with get_client(options.url, options.token) as client:
        challenge = control_plane.api(client, "POST", path)
        session_id = str(challenge.get("session_id") or "")
        if json_mode:
            _print_json(challenge)
        else:
            _render_login_qr(challenge)

        result = challenge
        status = str(challenge.get("status") or "pending")
        while status in {"pending", "scanned"} and time.monotonic() < deadline:
            time.sleep(wait_s)
            result = control_plane.api(client, "GET", f"{path}/{session_id}")
            next_status = str(result.get("status") or status)
            if not json_mode and next_status != status:
                typer.echo(f"status: {next_status}")
            status = next_status

        if status in {"confirmed", "success"}:
            if json_mode:
                _print_json(result)
            else:
                typer.echo(f"login confirmed: {ref}")
            return
        if status in {"pending", "scanned"}:
            # Timed out: best-effort cancel so the daemon stops the upstream QR task.
            with contextlib.suppress(ControlPlaneError):
                control_plane.api(client, "DELETE", f"{path}/{session_id}")
            typer.secho(f"error: login timed out after {timeout:g}s", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        if json_mode:
            _print_json(result)
        else:
            message = str(result.get("message") or "")
            typer.secho(
                f"error: login {status}" + (f": {message}" if message else ""),
                fg=typer.colors.RED,
                err=True,
            )
        raise typer.Exit(1)


def _render_login_qr(challenge: dict[str, Any]) -> None:
    """Render the QR challenge: encode text locally, or print the image URL."""
    content = str(challenge.get("qr_content") or "")
    image_url = str(challenge.get("qr_image_url") or "")
    if content:
        from im_gateway.cli.qr import render_qr

        typer.echo(render_qr(content))
        return
    if image_url:
        typer.echo(f"QR image: {image_url}")
        return
    typer.secho("error: 后端未返回二维码内容", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


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
