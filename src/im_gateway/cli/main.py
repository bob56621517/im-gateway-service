"""``imgw`` — the im-gateway command line client.

Two-face design (HANDOVER §5.6, §13.7): ``imgw run`` is the **only** command
that starts a process; every other command talks to the daemon's control-plane
HTTP API. SQLite is never touched directly (the daemon is the only writer).

Hard rule: this module must not import ``im_gateway.gateway.*`` or
``im_gateway.db.*``. The single allowed gateway import is the lazy one inside
:func:`run`.

User-facing prose goes through :func:`im_gateway.cli.i18n.t`.  The English
string is the message id, so ``--help`` text is written in English here and the
Chinese translation lives in the catalog in ``i18n.py``.  Machine-readable
output (JSON keys, TSV headers, ids, ``sent to ...`` / ``deleted: ...`` lines,
exit codes, masking) stays English in both languages.
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
from typing import Annotated, Any, Final, NoReturn, cast
from urllib.parse import quote

import httpx
import typer

from im_gateway.cli import client as control_plane
from im_gateway.cli.client import ControlPlaneError
from im_gateway.cli.i18n import t
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

CHANNEL_TABLE_HEADERS = ["CHANNEL_ID", "KIND", "ALIAS", "ENABLED", "STATE", "NAME"]
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


#: ``rich_markup_mode=None`` keeps Click's plain help formatter.  That formatter
#: honours the ``\b`` no-wrap paragraphs used for copy-pasteable examples; Rich
#: would re-wrap them (and would also parse ``[...]`` in prose as markup).
app = typer.Typer(
    name="imgw",
    help=t(
        "im-gateway control CLI: manage one im-gateway daemon, its channels, "
        "configuration and message history.\n"
        "\n"
        "Only `imgw run` and `imgw run-dev` start a process; every other command is "
        "an HTTP client for the daemon's control-plane API and never reads or writes "
        "the SQLite database directly.\n"
        "\n"
        "Output contract: data goes to stdout; progress, warnings and errors go to "
        "stderr. Run `imgw <command> --help` for per-command options and "
        "copy-pasteable examples."
    ),
    epilog=t(
        "Quick start:\n"
        "\n"
        "\b\n"
        "$ imgw run                                 # terminal 1: start the daemon\n"
        "$ imgw run --backend-url http://127.0.0.1:8080   # ...with an explicit backend\n"
        "$ imgw status                              # terminal 2: daemon + channel state\n"
        "$ imgw channel login feishu --name sales   # QR-code bind (5 platforms)\n"
        "$ imgw channel list                        # TSV: channel_id kind state creds alias\n"
        "$ imgw send sales ou_xxx 'hello'           # push one message\n"
        "\n"
        "Command groups:\n"
        "\n"
        "\b\n"
        "channel   login | logout | list | delete | test   bind and manage channel instances\n"
        "subject   ls                                      inspect known push targets\n"
        "config    export | import | validate              portable config document\n"
        "\n"
        "Environment variables:\n"
        "\n"
        "\b\n"
        "IMGW_URL         control-plane URL (http(s)://host:port or unix:///path/to.sock)\n"
        "IMGW_TOKEN       control-plane bearer token\n"
        "IMGW_TOKEN_FILE  file holding the bearer token (created by `imgw run`)\n"
        "IMGW_BACKEND_URL       agent backend base URL (default http://127.0.0.1:8080)\n"
        "IMGW_BACKEND_TOKEN     optional bearer token sent to the backend\n"
        "IMGW_BACKEND_TIMEOUT_MS  backend turn timeout in milliseconds (default 120000)\n"
        "IMGW_LANG        help/error language: en (default) or zh (zh_CN, zh-TW, GBK/936)\n"
        "\n"
        "Exit codes:\n"
        "\n"
        "\b\n"
        "0  success\n"
        "1  command failed (unreachable daemon, HTTP error, timeout, not ready)\n"
        "2  invalid command-line usage (reported by the CLI framework)\n"
        "\n"
        "Language:\n"
        "\n"
        "\b\n"
        "CLI help and progress/error text are English by default. They switch to\n"
        "Chinese when the OS locale is Chinese or IMGW_LANG=zh (zh_CN / zh-TW / GBK /\n"
        "936 are accepted; anything else means English). Machine-readable output --\n"
        "JSON keys, TSV headers, channel/subject ids, `sent to ...` and `deleted: ...`\n"
        "lines, masking (***), timestamps and exit codes -- stays English in both\n"
        "languages. Daemon-side error details are still Chinese for now."
    ),
    rich_markup_mode=None,
    no_args_is_help=True,
    add_completion=False,
)

channel_app = typer.Typer(
    name="channel",
    help=t(
        "Bind and manage channel instances.\n"
        "\n"
        "A channel is one platform instance: one set of credentials, one connection "
        "and one subject namespace.\n"
        "\n"
        "Use `imgw channel login` to create or re-bind a channel (QR scan, silent "
        "credentials or reconnect), `imgw channel list` to inspect channels, "
        "`imgw channel logout` to disable one while keeping its data, and "
        "`imgw channel delete` to remove it (it must be logged out first)."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw channel login feishu --name sales\n"
        "$ imgw channel list\n"
        "$ imgw channel logout sales\n"
        "$ imgw channel delete sales --yes\n"
    ),
    rich_markup_mode=None,
    no_args_is_help=True,
)
subject_app = typer.Typer(
    name="subject",
    help=t(
        "Inspect the known push targets (subjects).\n"
        "\n"
        "A subject is a conversation target -- a direct-message user or a group "
        "session; the daemon learns subjects from inbound messages."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw subject ls sales\n"
        "$ imgw subject ls feishu:acme:sales --json\n"
    ),
    rich_markup_mode=None,
    no_args_is_help=True,
)
config_app = typer.Typer(
    name="config",
    help=t(
        "Export, import or validate the config document.\n"
        "\n"
        "The database is the single source of truth for a running daemon; "
        "export/import is the supported way to move configuration between "
        "environments."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw config export -o cfg.json\n"
        "$ imgw config import cfg.json\n"
        "$ imgw config validate cfg.json\n"
    ),
    rich_markup_mode=None,
    no_args_is_help=True,
)
app.add_typer(channel_app, name="channel")
app.add_typer(subject_app, name="subject")
app.add_typer(config_app, name="config")


def _make_streams_encoding_safe() -> None:
    """Degrade unencodable characters instead of crashing on legacy consoles.

    Windows terminals often expose a GBK/CP936 stdout; printing a channel reply
    that contains emoji would otherwise raise ``UnicodeEncodeError`` from deep
    inside typer.  ``errors="replace"`` keeps the command working and renders
    the unencodable character as ``?``.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")


@app.callback()
def _root(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            envvar="IMGW_URL",
            help=t(
                "Control-plane URL: http(s)://host:port or unix:///path/to.sock "
                "(default: $IMGW_URL, then the daemon's default listen address)."
            ),
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            envvar="IMGW_TOKEN",
            help=t(
                "Control-plane bearer token (default: $IMGW_TOKEN, then the file "
                "named by $IMGW_TOKEN_FILE or the daemon token file)."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
    ] = False,
) -> None:
    """Global options shared by every command."""
    _make_streams_encoding_safe()
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
            raise typer.BadParameter(
                t("invalid --config {pair!r}: expected KEY=VALUE", pair=pair)
            )
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
        typer.echo(t("(dry run: nothing was written or executed)"))
        return
    if service_file is not None:
        typer.echo(t("service definition: {path}", path=service_file))
    typer.echo(t("ok: {count} command(s) executed", count=len(commands)))


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
                    t(
                        "the daemon did not become ready within {timeout}s: {error}",
                        timeout=f"{timeout_s:.0f}",
                        error=exc,
                    )
                ) from exc
            time.sleep(0.5)
        else:
            typer.echo(t("ready: the daemon answered /readyz"))
            return


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command(
    help=t(
        "Show the daemon and library versions: service, octop-gateway and Python "
        "(`GET /v1/version`). `--json` prints the raw version object."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw version\n"
        "$ imgw version --json\n"
    ),
    short_help=t('Show daemon, octop-gateway and Python versions.'),
)
@_handle_errors
def version(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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


@app.command(
    help=t(
        "Show daemon status: service and octop-gateway versions, uptime, database "
        "and media paths, the effective backend URL, and per-channel state "
        "(`GET /v1/status`)."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw status\n"
        "$ imgw status --json\n"
    ),
    short_help=t('Show daemon status and per-channel state.'),
)
@_handle_errors
def status(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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
        f"backend_url     {payload.get('backend_url', '-')}",
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


@app.command(
    help=t(
        "Probe the daemon's readiness endpoint (`GET /readyz`). Exit code 0 means "
        "ready; an unreachable or not-ready daemon exits 1, which makes this "
        "command usable in scripts and behind `imgw start --wait`."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw ready\n"
        "$ imgw ready && imgw send sales ou_xxx 'the daemon is up'\n"
    ),
    short_help=t('Exit 0 when the daemon is ready.'),
)
@_handle_errors
def ready(ctx: typer.Context) -> None:
    """Ask the daemon's /readyz; exit 0 when ready, 1 otherwise."""
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        try:
            payload = control_plane.api(client, "GET", "/readyz")
        except ControlPlaneError as exc:
            typer.secho(t("not ready: {error}", error=exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
    state = payload.get("status", "ready") if isinstance(payload, dict) else "ready"
    typer.echo(t("ready: {state}", state=state))


@app.command(
    help=t(
        "Push one text message to a subject (`POST /v1/messages`), or broadcast to "
        "every known subject of the channel with `--all` "
        "(`POST /v1/messages/broadcast`). A fresh Idempotency-Key is generated for "
        "every single-subject send. The confirmation line is always English for "
        "pipe stability: `sent to <channel>/<subject>`."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw send sales ou_xxx 'hello'\n"
        "$ imgw send sales --all 'maintenance at 22:00'\n"
        "$ imgw send feishu:acme:sales ou_xxx 'hello' --json\n"
    ),
    short_help=t('Push a message to one subject or broadcast.'),
)
@_handle_errors
def send(
    ctx: typer.Context,
    ref: Annotated[
        str,
        typer.Argument(help=t("channel_id or alias of the channel to send through.")),
    ],
    subject_id: Annotated[
        str | None,
        typer.Argument(help=t("Target subject_id (omit with --all).")),
    ] = None,
    text: Annotated[str | None, typer.Argument(help=t("Message text (plain text)."))] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help=t("Broadcast to every known subject of the channel.")),
    ] = False,
) -> None:
    """Push a text message to one subject, or broadcast with --all."""
    options = _options(ctx)
    if all_:
        # `imgw send <ref> --all <text>` leaves `<text>` in subject_id because
        # Typer fills positionals left to right.
        broadcast_text = text if text is not None else subject_id
        if not broadcast_text:
            raise typer.BadParameter(
                t("missing TEXT: usage is `imgw send <ref> --all <text>`")
            )
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
            t(
                "usage is `imgw send <ref> <subject_id> <text>` or "
                "`imgw send <ref> --all <text>`"
            )
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


@app.command(
    help=t(
        "Query the daemon's stored message history (`GET /v1/history`, read-only). "
        "Both inbound and outbound messages are shown with timestamps, direction "
        "and a truncated text preview."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw history --limit 20\n"
        "$ imgw history --channel sales --subject ou_xxx --since 1730000000\n"
        "$ imgw history --json\n"
    ),
    short_help=t('Query stored message history (read-only).'),
)
@_handle_errors
def history(
    ctx: typer.Context,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help=t("Filter by channel_id or alias.")),
    ] = None,
    subject: Annotated[str | None, typer.Option("--subject", help=t("Filter by subject_id."))] = None,
    since: Annotated[
        float | None,
        typer.Option("--since", help=t("Only messages at/after this epoch timestamp (seconds).")),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help=t("Maximum number of messages to return."))] = 50,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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
        typer.echo(t("(no messages)"))
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


@app.command(
    help=t(
        "Tail the daemon's SSE event stream (`GET /v1/events`) until Ctrl-C or the "
        "connection closes. Each stdout line is one JSON event; keepalive comments "
        "are dropped. Use `--channel` to filter."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw events\n"
        "$ imgw events --channel sales\n"
    ),
    short_help=t('Tail the SSE event stream.'),
)
@_handle_errors
def events(
    ctx: typer.Context,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help=t("Only events for this channel_id or alias.")),
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
            t(
                "cannot reach the im-gateway daemon: {error}. Start it with "
                "`imgw run`.",
                error=exc,
            )
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


@app.command(
    help=t(
        "Run control-plane self-checks: `/healthz`, `/readyz`, `/v1/version` and "
        "`/v1/status` are called in order and every result is printed. Exit code 0 "
        "when all checks pass, 1 otherwise."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw doctor\n"
        "$ imgw doctor --json\n"
    ),
    short_help=t('Run control-plane self-checks.'),
)
@_handle_errors
def doctor(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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
        typer.echo(t("all checks passed") if ok else t("doctor found problems"))
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


@app.command(
    help=t(
        "Start the gateway daemon in the foreground (the production entry point). "
        "This is the only command that starts a process: it migrates and opens the "
        "SQLite database, reconciles enabled channels from the database, and serves "
        "the control plane plus all channel connections in one asyncio loop. Every "
        "inbound turn is POSTed to one backend URL: `--backend-url`, then "
        "`$IMGW_BACKEND_URL`, then `http://127.0.0.1:8080`. Stop it with Ctrl-C; "
        "for a managed service use `imgw install-service` followed by `imgw start`."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw run\n"
        "$ imgw run --backend-url http://127.0.0.1:8080\n"
        "$ imgw run --host 127.0.0.1 --port 8765\n"
    ),
    short_help=t('Start the gateway daemon (production).'),
)
def run(
    host: Annotated[str, typer.Option("--host", help=t("Control-plane listen host."))] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help=t("Control-plane listen port."))] = 8765,
    backend_url: Annotated[
        str | None,
        typer.Option(
            "--backend-url",
            envvar="IMGW_BACKEND_URL",
            help=t(
                "Agent backend base URL; every turn is POSTed to "
                "`{url}/v1/turns` (default: $IMGW_BACKEND_URL, then "
                "http://127.0.0.1:8080)."
            ),
        ),
    ] = None,
) -> None:
    """Start the gateway daemon in the foreground (the production entry point)."""
    # TODO(milestone-2): also expose --db / --listen once the daemon accepts them.
    _start_daemon(host=host, port=port, backend_url=backend_url)


@app.command(
    "run-dev",
    help=t(
        "Start the daemon in development mode. On top of a normal run, an "
        "in-process echo backend (`im_gateway.dev.backend`) listens on "
        "`--dev-backend-port`, and the global backend URL is pointed at it (all "
        "other mechanisms — token, timeout, history, dedup — are unchanged). The "
        "echo backend replies to any inbound message with one `message` frame and "
        "one `completed` frame listing channel, subject, turn and idempotency ids, "
        "so channel connectivity can be verified without a real backend."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw run-dev\n"
        "$ imgw run-dev --port 8765 --dev-backend-port 9900\n"
        "$ imgw run-dev --host 0.0.0.0 --port 9000\n"
    ),
    short_help=t('Start the daemon with the in-process echo backend.'),
)
def run_dev(
    host: Annotated[str, typer.Option("--host", help=t("Control-plane listen host."))] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help=t("Control-plane listen port."))] = 8765,
    dev_backend_port: Annotated[
        int,
        typer.Option(
            "--dev-backend-port",
            help=t("Loopback port for the in-process echo backend."),
        ),
    ] = 9900,
) -> None:
    """Start the daemon in dev mode with the global backend URL on the echo backend.

    The echo backend (``im_gateway.dev.backend``) replies to any inbound message
    with a fixed text that lists channel / subject / turn / idempotency / text,
    so gateway–channel connectivity can be verified without a real backend.
    """
    _start_daemon(host=host, port=port, dev_backend=True, dev_backend_port=dev_backend_port)


def _start_daemon(*, host: str, port: int, **extra: Any) -> None:
    # Deliberate lazy import: the only place the CLI is allowed to touch the
    # gateway package, so plain HTTP commands never load the channel SDKs.
    try:
        from im_gateway.gateway.app import run_daemon
    except ImportError as exc:  # pragma: no cover - only when the daemon side is absent
        typer.secho(
            f"error: {t('the gateway runtime is not available: {error}', error=exc)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    run_daemon(host=host, port=port, **extra)


@app.command(
    "install-service",
    help=t(
        "Generate the platform service definition (`systemd` unit, `launchd` plist "
        "or NSSM commands) and install/enable it so the OS service manager keeps "
        "`imgw run` alive. The default is a user-scoped service; `--system` "
        "installs system-wide (needs root). `--print-only` prints the definition "
        "without writing or executing anything; `--dry-run` prints the commands "
        "without running them."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw install-service --print-only\n"
        "$ imgw install-service --dry-run\n"
        "$ imgw install-service --system --workdir /srv/im-gateway\n"
    ),
    short_help=t('Install the OS service definition.'),
)
@_handle_service_errors
def install_service(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help=t("Print the commands without writing files or running them.")),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help=t(
                "Install a user-scoped service (default, no root) or a system-wide "
                "one (needs root)."
            ),
        ),
    ] = True,
    workdir: Annotated[
        str | None,
        typer.Option(
            "--workdir",
            help=t("Working directory of the daemon (default: the current directory)."),
        ),
    ] = None,
    print_only: Annotated[
        bool,
        typer.Option(
            "--print-only",
            help=t("Print the generated definition without writing or executing anything."),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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


@app.command(
    "uninstall-service",
    help=t(
        "Stop and remove the platform service definition installed by "
        "`imgw install-service`. The default is the user-scoped service; "
        "`--system` targets the system-wide one."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw uninstall-service\n"
        "$ imgw uninstall-service --system\n"
        "$ imgw uninstall-service --dry-run\n"
    ),
    short_help=t('Remove the OS service definition.'),
)
@_handle_service_errors
def uninstall_service(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help=t("Print the commands without executing them.")),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help=t("Manage the user-scoped service (default) or the system-wide one."),
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
    ] = False,
) -> None:
    """Stop and remove the platform service definition."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    target = default_target(platform=platform, spec=spec)
    commands = ServiceController(platform=platform, spec=spec).uninstall(target=target, dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)


@app.command(
    "start",
    help=t(
        "Start the installed OS service through the platform service manager "
        "(systemd / launchd / NSSM). `--wait` additionally polls `/readyz` for up "
        "to 30 seconds and fails if the daemon does not become ready. The default "
        "is the user-scoped service."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw start\n"
        "$ imgw start --wait\n"
        "$ imgw start --system --dry-run\n"
    ),
    short_help=t('Start the installed OS service.'),
)
@_handle_service_errors
def start(
    ctx: typer.Context,
    wait: Annotated[
        bool,
        typer.Option("--wait", help=t("Poll /readyz until the daemon is ready.")),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help=t("Print the commands without executing them.")),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help=t("Manage the user-scoped service (default) or the system-wide one."),
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
    ] = False,
) -> None:
    """Start the system service."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    commands = ServiceController(platform=platform, spec=spec).start(dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)
    if wait and not dry_run:
        _wait_ready(ctx)


@app.command(
    "stop",
    help=t(
        "Stop the installed OS service through the platform service manager. The "
        "default is the user-scoped service; `--system` targets the system-wide "
        "one."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw stop\n"
        "$ imgw stop --system\n"
    ),
    short_help=t('Stop the installed OS service.'),
)
@_handle_service_errors
def stop(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help=t("Print the commands without executing them.")),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help=t("Manage the user-scoped service (default) or the system-wide one."),
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
    ] = False,
) -> None:
    """Stop the system service."""
    platform = _service_platform()
    spec = _service_spec(workdir=None, user_scope=user_scope)
    commands = ServiceController(platform=platform, spec=spec).stop(dry_run=dry_run)
    _emit_service_result(ctx, commands, dry_run=dry_run, json_output=json_output)


@app.command(
    "restart",
    help=t(
        "Restart the installed OS service through the platform service manager. "
        "The default is the user-scoped service; `--system` targets the "
        "system-wide one."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw restart\n"
        "$ imgw restart --system\n"
    ),
    short_help=t('Restart the installed OS service.'),
)
@_handle_service_errors
def restart(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help=t("Print the commands without executing them.")),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user/--system",
            help=t("Manage the user-scoped service (default) or the system-wide one."),
        ),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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

#: Default overall login wait (15 minutes).  It is also the QR validity where
#: the platform honours a wait knob; platforms that decide their own TTL report
#: it back and the wait is clamped to it.
DEFAULT_LOGIN_TIMEOUT_S: Final[float] = 900.0


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
            _fail(t("--secret-stdin: no secret found on stdin"))
    if app_id:
        values["app_id"] = app_id
    if app_secret:
        # QQ's channel config names the app secret ``secret``; other kinds use
        # ``app_secret`` (upstream ``QQConfig.required_credentials``).
        values["secret" if kind == "qq" else "app_secret"] = app_secret
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
        if kind in {"wecom", "qq"}:
            values["secret"] = stdin_secret
        elif kind in {"feishu", "dingtalk"}:
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
        _fail(t("the daemon did not return QR content; cannot write the PNG"))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_qr_bytes(text))
    _stderr(t("QR PNG: {path}", path=target))


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
    _stderr(
        t(
            "(this terminal cannot encode QR characters; printing the URL only -- "
            "use --qr-png for an image)"
        )
    )
    typer.echo(content)


def _emit_login_challenge(challenge: dict[str, Any], output_format: str) -> None:
    text = _login_qr_text(challenge)
    if not text:
        _fail(t("the daemon did not return QR content"))
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


def _login_app_name(kind: str) -> str:
    """Localized name of the app the user must scan with."""
    if kind == "weixin":
        return t("WeChat")
    if kind == "wecom":
        return t("WeCom")
    if kind == "dingtalk":
        return t("DingTalk")
    if kind == "feishu":
        return t("Feishu")
    if kind == "qq":
        return t("QQ")
    return t("this app")


def _format_login_ttl(seconds: float) -> str:
    """Human duration for the QR-validity hint (localized)."""
    total = round(seconds)
    if total < 60:
        return t("1 second") if total == 1 else t("{value} seconds", value=total)
    minutes, rest = divmod(total, 60)
    if rest == 0:
        return t("1 minute") if minutes == 1 else t("{value} minutes", value=minutes)
    return t("{minutes} minutes {seconds} seconds", minutes=minutes, seconds=rest)


def _emit_login_hint(challenge: dict[str, Any], output_format: str) -> None:
    """One-line scan hint: app, optional platform validity, and the fallback link."""
    if output_format == "json":
        return  # keep the NDJSON stream free of prose
    kind = str(challenge.get("kind") or "")
    app = _login_app_name(kind)
    url = _login_qr_text(challenge)
    ttl = challenge.get("expires_in_s")
    if isinstance(ttl, int | float) and not isinstance(ttl, bool) and ttl > 0:
        _stderr(
            t(
                "Scan the QR code with {app} (valid for {ttl}), or open this link in {app}: {url}",
                app=app,
                ttl=_format_login_ttl(float(ttl)),
                url=url,
            )
        )
        return
    _stderr(
        t(
            "Scan the QR code with {app}, or open this link in {app}: {url}",
            app=app,
            url=url,
        )
    )


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
        challenge = control_plane.api(client, "POST", path, json_body={"timeout_s": timeout})
        sid = str(challenge.get("session_id") or "")
        if not sid:
            _fail(t("the daemon did not return a login session_id"))
        if qr_png:
            _write_login_qr_png(challenge, qr_png)
        _emit_login_challenge(challenge, output_format)
        _emit_login_hint(challenge, output_format)
    sid = str(challenge.get("session_id") or "")
    if not sid:
        _fail(t("the daemon did not return a login session_id"))
    if no_wait:
        _stderr(
            t(
                "session: {session_id} (resume with --session-id {session_id})",
                session_id=sid,
            )
        )
        return challenge
    wait_s = max(0.1, timeout)
    ttl = challenge.get("expires_in_s")
    if isinstance(ttl, int | float) and not isinstance(ttl, bool) and ttl > 0:
        # Platform-decided QR TTL: no point waiting long past it.
        wait_s = min(wait_s, float(ttl) + 5.0)
    deadline = time.monotonic() + wait_s
    status = str(challenge.get("status") or "pending")
    result: dict[str, Any] = challenge
    while status in {"pending", "scanned"} and time.monotonic() < deadline:
        time.sleep(max(0.1, interval))
        result = control_plane.api(client, "GET", f"{path}/{_ref_path(sid)}")
        next_status = str(result.get("status") or status)
        if next_status != status and output_format != "json":
            _stderr(t("status: {status}", status=next_status))
        status = next_status
    if status in {"confirmed", "success"}:
        if output_format == "json":
            typer.echo(_compact_json({"event": "result", **result}))
        _stderr(t("login confirmed: {ref}", ref=ref))
        message = str(result.get("message") or "")
        if message:
            _stderr(t("warning: {message}", message=message))
        return result
    if status in {"pending", "scanned"}:
        with contextlib.suppress(ControlPlaneError):
            control_plane.api(client, "DELETE", f"{path}/{_ref_path(sid)}")
        _fail(t("login timed out after {timeout:g}s", timeout=timeout))
    if output_format == "json":
        typer.echo(_compact_json({"event": "result", **result}))
    message = str(result.get("message") or "")
    reason = f": {message}" if message else ""
    _fail(t("login {status}{reason}", status=status, reason=reason))


def _report_logged_in(channel: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        typer.echo(_compact_json({"event": "result", **channel}))
        return
    _stderr(
        t(
            "logged in: {channel_id} ({state})",
            channel_id=channel.get("channel_id"),
            state=channel.get("state"),
        )
    )


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


@channel_app.command(
    "login",
    help=t(
        "Bind or re-bind a channel instance: scan a QR code, pass credentials "
        "silently, or reconnect an already-bound channel.\n"
        "\n"
        "TARGET is either an existing channel_id/alias to re-bind, or a channel "
        "kind (feishu/wecom/dingtalk/qq/weixin) to create a new instance.\n"
        "\n"
        "Behavior matrix:\n"
        "\n"
        "\b\n"
        "new target, no credentials         QR login (auto-enabled on confirmation)\n"
        "new target, credential flags       silent registration (no QR, enabled immediately)\n"
        "existing target, credentials ok    reconnect / re-enable without scanning\n"
        "existing target, credentials lost  QR login\n"
        "existing target, credential flags  overwrite credentials, then reconnect\n"
        "existing target + --rescan         force a fresh QR scan (expired token, new account)\n"
        "\n"
        "Per-platform credential flags:\n"
        "\n"
        "\b\n"
        "feishu    --app-id / --app-secret\n"
        "wecom     --bot-id / --secret\n"
        "dingtalk  --app-key / --app-secret\n"
        "qq        --app-id / --secret        (QQ stores the secret as `secret`)\n"
        "weixin    --token / --account-id / --base-url   (also builds an `accounts` entry)\n"
        "\n"
        "Use --secret-stdin to read the secret from stdin instead of argv (keeps it "
        "out of the process list), --check to probe new credentials before "
        "registering, and --no-wait to print the QR challenge immediately and "
        "resume later with --session-id."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "# 1. create a channel by QR scan (prints URL + terminal QR; also writes a PNG)\n"
        "$ imgw channel login weixin:wx --qr-png ./weixin-qr.png\n"
        "\n"
        "\b\n"
        "# 2. create a channel silently from credentials (no scan)\n"
        "$ imgw channel login feishu --id feishu:acme:sales --name sales \\\n"
        "    --app-id cli_xxx --app-secret \"$FEISHU_APP_SECRET\"\n"
        "\n"
        "\b\n"
        "# 3. overwrite the credentials of an existing channel and reconnect\n"
        "$ imgw channel login feishu:acme:sales --app-id cli_yyy \\\n"
        "    --app-secret \"$FEISHU_APP_SECRET\"\n"
        "\n"
        "\b\n"
        "# 4. force a fresh scan (e.g. an expired WeChat session)\n"
        "$ imgw channel login weixin:wx --rescan\n"
        "\n"
        "\b\n"
        "# 5. scripted flow: print the URL and exit, then resume with --session-id\n"
        "$ imgw channel login weixin:wx --format url --no-wait\n"
        "$ imgw channel login weixin:wx --session-id abc123 --format json\n"
    ),
    short_help=t('Bind or re-bind a channel (QR, credentials, reconnect).'),
)
@_handle_errors
def channel_login(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Argument(
            help=t(
                "Existing channel_id/alias to re-bind, or a channel kind "
                "(feishu/wecom/dingtalk/qq/weixin) to create a new channel."
            )
        ),
    ],
    channel_id: Annotated[
        str,
        typer.Option(
            "--id",
            help=t("Immutable channel_id for a new channel (default: <kind>:<name>)."),
        ),
    ] = "",
    name: Annotated[
        str,
        typer.Option(
            "--name",
            help=t("Instance name for a new channel (also used as the CLI alias)."),
        ),
    ] = "",
    config: Annotated[
        list[str] | None,
        typer.Option(
            "--config",
            help=t(
                "Extra channel config as KEY=VALUE; repeatable (values are typed as "
                "bool/int/string)."
            ),
        ),
    ] = None,
    app_id: Annotated[str, typer.Option("--app-id", help=t("feishu/qq: app_id."))] = "",
    app_secret: Annotated[
        str,
        typer.Option("--app-secret", help=t("feishu/qq/dingtalk: app_secret (QQ stores it as `secret`).")),
    ] = "",
    bot_id: Annotated[str, typer.Option("--bot-id", help=t("wecom: bot_id."))] = "",
    secret: Annotated[str, typer.Option("--secret", help=t("wecom/qq: bot or application secret."))] = "",
    app_key: Annotated[str, typer.Option("--app-key", help=t("dingtalk: app_key (client_id)."))] = "",
    token: Annotated[
        str,
        typer.Option("--token", help=t("qq: bot token, or weixin: account bot token.")),
    ] = "",
    account_id: Annotated[
        str,
        typer.Option("--account-id", help=t("weixin: account id (also fills the `accounts` entry).")),
    ] = "",
    base_url: Annotated[str, typer.Option("--base-url", help=t("weixin: API base URL."))] = "",
    secret_stdin: Annotated[
        bool,
        typer.Option("--secret-stdin", help=t("Read the secret from stdin instead of argv.")),
    ] = False,
    rescan: Annotated[
        bool,
        typer.Option("--rescan", help=t("Force a fresh QR scan even when credentials exist.")),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help=t("Probe credentials before registering a new channel.")),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            help=t(
                "QR output: both (URL + terminal QR), url (URL only), qr (terminal "
                "QR only) or json (NDJSON events)."
            ),
        ),
    ] = "both",
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Alias for --format json (NDJSON events).")),
    ] = False,
    qr_png: Annotated[
        str,
        typer.Option("--qr-png", help=t("Also write the QR code to this PNG path.")),
    ] = "",
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help=t(
                "Overall login wait in seconds (default 15 minutes).  Also the QR validity "
                "where the platform honours it; platforms with their own TTL report it and "
                "the wait is clamped to that value."
            ),
        ),
    ] = DEFAULT_LOGIN_TIMEOUT_S,
    interval: Annotated[
        float,
        typer.Option("--interval", help=t("Poll interval while waiting for confirmation (seconds).")),
    ] = 2.0,
    no_wait: Annotated[
        bool,
        typer.Option(
            "--no-wait",
            help=t("Print the challenge and exit; resume with --session-id."),
        ),
    ] = False,
    session_id: Annotated[
        str,
        typer.Option(
            "--session-id",
            help=t("Resume an existing login session for the same channel ref."),
        ),
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
        _fail(
            t(
                "unsupported --format {value!r}: choose {choices}",
                value=output_format,
                choices="/".join(_LOGIN_FORMATS),
            )
        )
    if timeout <= 0:
        _fail(t("--timeout must be a positive number"))
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
                _fail(t("--session-id can only be used with an existing channel"))
            alias = name or None
            new_id = channel_id or (f"{target}:{alias}" if alias else f"{target}:{uuid.uuid4().hex[:6]}")
            entry = ChannelConfigEntry(
                channel_id=new_id,
                kind=target,
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
            _stderr(
                t(
                    "created {channel_id} (disabled until the QR login completes)",
                    channel_id=new_id,
                )
            )
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
                    t(
                        "channel {ref!r} ({kind}) does not support QR login; pass "
                        "credentials instead (--app-id/--app-secret, ...)",
                        ref=ref,
                        kind=existing.get("kind"),
                    )
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


@channel_app.command(
    "logout",
    help=t(
        "Log a channel out: disable it while keeping its credentials, subjects, "
        "message history and config. This is the old `disable` semantic and is "
        "idempotent. Re-bind with `imgw channel login <ref>` (no QR scan is needed "
        "while credentials remain)."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw channel logout sales\n"
        "$ imgw channel logout feishu:acme:sales --json\n"
    ),
    short_help=t('Disable a channel, keeping its data.'),
)
@_handle_errors
def channel_logout(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help=t("channel_id or alias."))],
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
    ] = False,
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
        t(
            "logged out: {channel_id} (disabled; credentials and history kept)",
            channel_id=payload.get("channel_id"),
        )
    )


@channel_app.command(
    "list",
    help=t(
        "List channels, or show one channel in detail.\n"
        "\n"
        "Without REF: one TSV row per channel with the fixed columns "
        "`channel_id  kind  state  creds  alias` (a header is printed by default; "
        "`--no-header` prints bare rows for pipes). Disabled channels are "
        "included.\n"
        "\n"
        "With REF: a `key<TAB>value` detail view, including login support, "
        "credential status and the masked config (`app_secret` / `token` are always "
        "`***`; `${ENV}` references are preserved). Secrets never leave the daemon."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw channel list\n"
        "$ imgw channel list | awk -F'\\t' '$3 == \"running\" {print $1}'\n"
        "$ imgw channel list --no-header | cut -f1\n"
        "$ imgw channel list sales\n"
    ),
    short_help=t('List channels or show one in detail.'),
)
@_handle_errors
def channel_list(
    ctx: typer.Context,
    ref: Annotated[
        str | None,
        typer.Argument(help=t("Optional channel_id/alias to show one channel in detail.")),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of TSV.")),
    ] = False,
    header: Annotated[
        bool,
        typer.Option(
            "--header/--no-header",
            help=t(
                "Print the TSV header line (default: on; use --no-header for bare rows)."
            ),
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
        _stderr(t("(no channels; bind one with `imgw channel login <kind>`)"))


@channel_app.command(
    "delete",
    help=t(
        "Delete a channel row (`DELETE /v1/channels/{ref}`). The channel must be "
        "logged out first: the daemon rejects deletion of an enabled channel with "
        "HTTP 409, and the CLI checks before calling. By default `subjects` and "
        "`messages` are kept; `--purge` deletes them too. `--yes` skips the "
        "interactive confirmation (required when stdin is not a TTY)."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw channel logout sales\n"
        "$ imgw channel delete sales --yes\n"
        "$ imgw channel delete sales --yes --purge\n"
        "$ imgw channel delete sales --yes --json\n"
    ),
    short_help=t('Delete a channel (log it out first).'),
)
@_handle_errors
def channel_delete(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help=t("channel_id or alias."))],
    purge: Annotated[
        bool,
        typer.Option("--purge", help=t("Also delete the channel's subjects and messages.")),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help=t("Skip the interactive confirmation.")),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
    ] = False,
) -> None:
    """Delete a channel row; the channel must be logged out first."""
    if not yes:
        if not sys.stdin.isatty():
            _fail(
                t(
                    "refusing to delete without confirmation; re-run with --yes "
                    "(stdin is not a TTY)"
                )
            )
        typer.confirm(t("delete channel {ref!r}?", ref=ref), abort=True)
    options = _options(ctx)
    with get_client(options.url, options.token) as client:
        current = _channel_or_none(client, ref)
        if current is None:
            _fail(t("unknown channel: {ref!r}", ref=ref))
        if current.get("enabled"):
            _fail(
                t(
                    "channel {channel_id!r} is still logged in; run "
                    "`imgw channel logout {channel_id}` first",
                    channel_id=current.get("channel_id"),
                )
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


@channel_app.command(
    "test",
    help=t(
        "Probe channel credentials without registering a channel: the daemon "
        "starts a temporary instance, validates the credentials and tears it down "
        "(`POST /v1/channels/probe`). Use it before a silent `channel login` or "
        "when debugging bad credentials."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw channel test feishu --config app_id=cli_xxx --config app_secret=\"$FEISHU_APP_SECRET\"\n"
        "$ imgw channel test wecom --config bot_id=\"$WECOM_BOT_ID\" --config secret=\"$WECOM_SECRET\"\n"
    ),
    short_help=t('Probe credentials without registering.'),
)
@_handle_errors
def channel_test(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help=t("Channel kind to probe, e.g. feishu."))],
    config: Annotated[
        list[str] | None,
        typer.Option(
            "--config",
            help=t("Channel config as KEY=VALUE; repeatable."),
        ),
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


@subject_app.command(
    "ls",
    help=t(
        "List the known push targets (subjects) of a channel "
        "(`GET /v1/channels/{ref}/subjects`). Subjects are learned from inbound "
        "messages; each row shows subject_id, chat_type, display_name, first_seen "
        "and last_seen."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw subject ls sales\n"
        "$ imgw subject ls feishu:acme:sales --json\n"
    ),
    short_help=t("List a channel's known push targets."),
)
@_handle_errors
def subject_ls(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help=t("channel_id or alias."))],
    json_output: Annotated[
        bool,
        typer.Option("--json", help=t("Emit JSON (machine-readable) instead of human-readable output.")),
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
        typer.echo(t("(no known subjects yet)"))
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


@config_app.command(
    "export",
    help=t(
        "Export the portable config document as JSON (`GET /v1/config/export`). "
        "Secrets are masked by default: literal secrets become `***`, while "
        "`${ENV}` references stay as-is. `--with-secrets` includes literal secrets "
        "(handle the output carefully); `-o/--output` writes a file instead of "
        "stdout."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw config export\n"
        "$ imgw config export -o cfg.json\n"
        "$ imgw config export --with-secrets -o cfg.secret.json\n"
    ),
    short_help=t('Export the config document (secrets masked).'),
)
@_handle_errors
def config_export(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help=t("Write the document to this file instead of stdout.")),
    ] = None,
    with_secrets: Annotated[
        bool,
        typer.Option("--with-secrets", help=t("Include literal secrets instead of masking them.")),
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
        typer.secho(
            f"error: {t('cannot write {path}: {error}', path=output, error=exc)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(t("exported config to {path}", path=output))


@config_app.command(
    "import",
    help=t(
        "Validate and import a config document (`POST /v1/config/import`) as a "
        "single daemon-side transaction: an invalid document changes nothing. "
        "Masked values (`***`) in the file mean 'keep the current value'."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw config import cfg.json\n"
        "$ imgw --json config import cfg.json\n"
    ),
    short_help=t('Import a config document in one transaction.'),
)
@_handle_errors
def config_import(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help=t("Config document to import.")),
    ],
) -> None:
    """Validate and import a config document in one daemon-side transaction."""
    try:
        document = ConfigDocument.model_validate_json(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.secho(
            f"error: {t('invalid config document {path}: {error}', path=file, error=exc)}",
            fg=typer.colors.RED,
            err=True,
        )
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
        f"settings={applied.get('settings', 0)}"
    )


@config_app.command(
    "validate",
    help=t(
        "Validate a config document locally against the pydantic schema; no daemon "
        "is required. Useful in CI and pre-commit hooks."
    ),
    epilog=t(
        "Examples:\n"
        "\n"
        "\b\n"
        "$ imgw config validate cfg.json\n"
        "$ imgw config export -o cfg.json && imgw config validate cfg.json\n"
    ),
    short_help=t('Validate a config document locally.'),
)
def config_validate(
    file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help=t("Config document to validate.")),
    ],
) -> None:
    """Validate a config document locally (no daemon required)."""
    try:
        document = ConfigDocument.model_validate_json(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.secho(
            f"error: {t('invalid config document {path}: {error}', path=file, error=exc)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(
        t(
            "config OK: {channels} channel(s), {settings} setting(s)",
            channels=len(document.channels),
            settings=len(document.settings),
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point (``imgw``)."""
    app()
