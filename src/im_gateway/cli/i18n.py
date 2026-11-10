"""Minimal, stdlib-only internationalization for the ``imgw`` CLI.

Design (deliberately small so no build step, ``gettext`` tooling or ``.mo``
files are needed):

* English is the **source language**: a "message id" (msgid) *is* the English
  text, so the English catalog is the identity mapping and an unknown msgid
  still prints readable English.  The Chinese catalog maps the same msgids to
  Chinese.
* The language is resolved **once at import time** because Typer/Click build
  every ``--help`` string at import time.  Tests can control it by setting
  ``IMGW_LANG`` before importing :mod:`im_gateway.cli.main`, by reloading the
  modules, or by calling :func:`set_language`.
* Detection order: ``IMGW_LANG`` > ``LC_ALL`` > ``LC_MESSAGES`` > ``LANG`` >
  OS localization (Windows UI language via ``GetUserDefaultUILanguage``; other
  platforms via :func:`locale.getlocale`) > English.
* Only user-facing prose is translated; machine-readable output (JSON keys,
  TSV headers, identifiers, exit codes, masking) stays English in both
  languages.

The Chinese catalog is grouped by source module and kept in source order.  The
``tests/test_i18n.py`` coverage test is the safety net: every ``t("...")``
literal in :mod:`im_gateway.cli` must have a Chinese entry.
"""

from __future__ import annotations

import locale
import os
import sys
from collections.abc import Mapping
from typing import Final, Literal

__all__ = [
    "CATALOGS",
    "Language",
    "detect_language",
    "get_language",
    "set_language",
    "t",
]

Language = Literal["en", "zh"]

#: Chinese catalog — keyed by the English msgid used in ``im_gateway.cli``.
_ZH_CATALOG: Final[dict[str, str]] = {
    # --- src/im_gateway/cli/client.py ---
    # client.py:45
    (
        'start it with `imgw run` (or check --url / IMGW_URL)'
    ): (
        '启动它请用 `imgw run`（或检查 --url / IMGW_URL）'
    ),
    # client.py:46
    (
        'no control-plane token found; start the daemon with `imgw run` so it creates the token '
        'file, or pass --token / set IMGW_TOKEN'
    ): (
        '未找到控制面 token；请先用 `imgw run` 启动守护进程以生成 token 文件，或通过 --token / '
        'IMGW_TOKEN 传入'
    ),
    # client.py:115
    'cannot read control-plane token {path}: {error}. {hint}': '无法读取控制面 token {path}：{error}。{hint}',
    # client.py:124
    'control-plane token file {path} is empty. {hint}': '控制面 token 文件 {path} 为空。{hint}',
    # client.py:156
    (
        'invalid control-plane URL: unix:// address has no socket path'
    ): (
        '控制面 URL 无效：unix:// 地址缺少 socket 路径'
    ),
    # client.py:195
    (
        'cannot reach the im-gateway daemon at {url}: {error}. {hint}'
    ): (
        '无法连接 im-gateway 守护进程 {url}：{error}。{hint}'
    ),
    # client.py:211
    (
        '{method} {path} failed with HTTP {status}: {detail}'
    ): (
        '{method} {path} 请求失败，HTTP {status}：{detail}'
    ),
    # client.py:227
    'daemon returned a non-JSON body for {path}: {body}': '守护进程对 {path} 返回了非 JSON 响应：{body}',

    # --- src/im_gateway/cli/main.py ---
    # main.py:78
    (
        'im-gateway control CLI: manage one im-gateway daemon, its channels, configuration and '
        'message history.\n'
        '\n'
        'Only `imgw run` and `imgw run-dev` start a process; every other command is an HTTP '
        "client for the daemon's control-plane API and never reads or writes the SQLite database "
        'directly.\n'
        '\n'
        'Output contract: data goes to stdout; progress, warnings and errors go to stderr. Run '
        '`imgw <command> --help` for per-command options and copy-pasteable examples.'
    ): (
        'im-gateway 控制台 CLI：管理一个 im-gateway 守护进程及其通道、配置与消息历史。\n'
        '\n'
        '只有 `imgw run` 与 `imgw run-dev` 会启动进程；其余命令都是守护进程控制面 API 的 HTTP '
        '客户端，绝不直接读写 SQLite 数据库。\n'
        '\n'
        '输出约定：数据写入 stdout，进度、告警和错误写入 '
        'stderr。每个命令的选项与可直接复制的示例见 `imgw <command> --help`。'
    ),
    # main.py:90
    (
        'Quick start:\n'
        '\n'
        '\b\n'
        '$ imgw run                                 # terminal 1: start the daemon\n'
        '$ imgw run --backend-url http://127.0.0.1:8080   # ...with an explicit backend\n'
        '$ imgw status                              # terminal 2: daemon + channel state\n'
        '$ imgw channel login feishu --name sales   # QR-code bind (5 platforms)\n'
        '$ imgw channel list                        # TSV: channel_id kind state creds alias\n'
        "$ imgw send sales ou_xxx 'hello'           # push one message\n"
        '\n'
        'Command groups:\n'
        '\n'
        '\b\n'
        'channel   login | logout | list | delete | test   bind and manage channel instances\n'
        'subject   ls                                      inspect known push targets\n'
        'config    export | import | validate              portable config document\n'
        '\n'
        'Environment variables:\n'
        '\n'
        '\b\n'
        'IMGW_URL         control-plane URL (http(s)://host:port or unix:///path/to.sock)\n'
        'IMGW_TOKEN       control-plane bearer token\n'
        'IMGW_TOKEN_FILE  file holding the bearer token (created by `imgw run`)\n'
        'IMGW_BACKEND_URL       agent backend base URL (default http://127.0.0.1:8080)\n'
        'IMGW_BACKEND_TOKEN     optional bearer token sent to the backend\n'
        'IMGW_BACKEND_TIMEOUT_MS  backend turn timeout in milliseconds (default 120000)\n'
        'IMGW_LANG        help/error language: en (default) or zh (zh_CN, zh-TW, GBK/936)\n'
        '\n'
        'Exit codes:\n'
        '\n'
        '\b\n'
        '0  success\n'
        '1  command failed (unreachable daemon, HTTP error, timeout, not ready)\n'
        '2  invalid command-line usage (reported by the CLI framework)\n'
        '\n'
        'Language:\n'
        '\n'
        '\b\n'
        'CLI help and progress/error text are English by default. They switch to\n'
        'Chinese when the OS locale is Chinese or IMGW_LANG=zh (zh_CN / zh-TW / GBK /\n'
        '936 are accepted; anything else means English). Machine-readable output --\n'
        'JSON keys, TSV headers, channel/subject ids, `sent to ...` and `deleted: ...`\n'
        'lines, masking (***), timestamps and exit codes -- stays English in both\n'
        'languages. Daemon-side error details are still Chinese for now.'
    ): (
        '快速开始：\n'
        '\n'
        '\b\n'
        '$ imgw run                                 # 终端 1：启动守护进程\n'
        '$ imgw run --backend-url http://127.0.0.1:8080   # ……显式指定后端\n'
        '$ imgw status                              # 终端 2：守护进程与通道状态\n'
        '$ imgw channel login feishu --name sales   # 扫码绑定（支持 5 个平台）\n'
        '$ imgw channel list                        # TSV：channel_id kind state creds alias\n'
        "$ imgw send sales ou_xxx 'hello'           # 推送一条消息\n"
        '\n'
        '命令分组：\n'
        '\n'
        '\b\n'
        'channel   login | logout | list | delete | test   绑定与管理通道实例\n'
        'subject   ls                                      查看已知推送目标\n'
        'config    export | import | validate              可移植配置文档\n'
        '\n'
        '环境变量：\n'
        '\n'
        '\b\n'
        'IMGW_URL         控制面 URL（http(s)://host:port 或 unix:///path/to/sock）\n'
        'IMGW_TOKEN       控制面 bearer token\n'
        'IMGW_TOKEN_FILE  保存 bearer token 的文件（由 `imgw run` 创建）\n'
        'IMGW_BACKEND_URL       后端基础 URL（默认 http://127.0.0.1:8080）\n'
        'IMGW_BACKEND_TOKEN     可选：发送给后端的 bearer token\n'
        'IMGW_BACKEND_TIMEOUT_MS  后端 turn 超时毫秒数（默认 120000）\n'
        'IMGW_LANG        帮助/错误语言：en（默认）或 zh（接受 zh_CN、zh-TW、GBK/936）\n'
        '\n'
        '退出码：\n'
        '\n'
        '\b\n'
        '0  成功\n'
        '1  命令失败（守护进程不可达、HTTP 错误、超时、未就绪）\n'
        '2  命令行用法错误（由 CLI 框架报告）\n'
        '\n'
        '语言：\n'
        '\n'
        '\b\n'
        'CLI 帮助与进度/错误文本默认英文。当操作系统区域设置为中文或设置 IMGW_LANG=zh\n'
        '时切换为中文（接受 zh_CN / zh-TW / GBK / 936；其他值一律视为英文）。机器可读\n'
        '输出 —— JSON 键、TSV 表头、channel/subject id、`sent to ...` 与 `deleted: ...`\n'
        '行、掩码（***）、时间戳和退出码 —— 两种语言下都保持英文。守护进程侧的错误\n'
        '详情目前仍为中文。'
    ),
    # main.py:139
    (
        'Bind and manage channel instances.\n'
        '\n'
        'A channel is one platform instance: one set of credentials, one connection and one '
        'subject namespace.\n'
        '\n'
        'Use `imgw channel login` to create or re-bind a channel (QR scan, silent credentials or '
        'reconnect), `imgw channel list` to inspect channels, `imgw channel logout` to disable '
        'one while keeping its data, and `imgw channel delete` to remove it (it must be logged '
        'out first).'
    ): (
        '绑定与管理通道实例。\n'
        '\n'
        '一个通道就是一个平台实例：一套凭据、一条连接、一个 subject 命名空间。\n'
        '\n'
        '用 `imgw channel login` 创建或重新绑定通道（扫码、静默凭据或重连），用 `imgw channel '
        'list` 查看通道，用 `imgw channel logout` 停用但保留数据，用 `imgw channel delete` '
        '删除（必须先登出）。'
    ),
    # main.py:150
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw channel login feishu --name sales\n'
        '$ imgw channel list\n'
        '$ imgw channel logout sales\n'
        '$ imgw channel delete sales --yes\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw channel login feishu --name sales\n'
        '$ imgw channel list\n'
        '$ imgw channel logout sales\n'
        '$ imgw channel delete sales --yes\n'
        ''
    ),
    # main.py:164
    (
        'Inspect the known push targets (subjects).\n'
        '\n'
        'A subject is a conversation target -- a direct-message user or a group session; the '
        'daemon learns subjects from inbound messages.'
    ): (
        '查看已知的推送目标（subjects）。\n'
        '\n'
        'subject 是一个会话目标——私聊用户或群会话；守护进程会从入站消息中学习 subject。'
    ),
    # main.py:170
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw subject ls sales\n'
        '$ imgw subject ls feishu:acme:sales --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw subject ls sales\n'
        '$ imgw subject ls feishu:acme:sales --json\n'
        ''
    ),
    # main.py:182
    (
        'Export, import or validate the config document.\n'
        '\n'
        'The database is the single source of truth for a running daemon; export/import is the '
        'supported way to move configuration between environments.'
    ): (
        '导出、导入或校验配置文档。\n'
        '\n'
        '数据库是运行中守护进程的唯一权威；export/import 是跨环境迁移配置的受支持方式。'
    ),
    # main.py:189
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw config export -o cfg.json\n'
        '$ imgw config import cfg.json\n'
        '$ imgw config validate cfg.json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw config export -o cfg.json\n'
        '$ imgw config import cfg.json\n'
        '$ imgw config validate cfg.json\n'
        ''
    ),
    # main.py:229
    (
        'Control-plane URL: http(s)://host:port or unix:///path/to.sock (default: $IMGW_URL, then'
        ' '
        "the daemon's default listen address)."
    ): (
        '控制面 URL：http(s)://host:port 或 unix:///path/to.sock（默认取 '
        '$IMGW_URL，其次取守护进程的默认监听地址）。'
    ),
    # main.py:240
    (
        'Control-plane bearer token (default: $IMGW_TOKEN, then the file named by '
        '$IMGW_TOKEN_FILE or the daemon token file).'
    ): (
        '控制面 bearer token（默认取 $IMGW_TOKEN，其次取 $IMGW_TOKEN_FILE 指向的文件或守护进程 '
        'token 文件）。'
    ),
    # main.py:248
    (
        'Emit JSON (machine-readable) instead of human-readable output.'
    ): (
        '输出 JSON（机器可读）而不是人类可读文本。'
    ),
    # main.py:422
    'invalid --config {pair!r}: expected KEY=VALUE': '无效的 --config {pair!r}：应为 KEY=VALUE',
    # main.py:488
    '(dry run: nothing was written or executed)': '（dry run：未写入或执行任何内容）',
    # main.py:491
    'service definition: {path}': '服务定义：{path}',
    # main.py:492
    'ok: {count} command(s) executed': 'ok：已执行 {count} 条命令',
    # main.py:506
    'the daemon did not become ready within {timeout}s: {error}': '守护进程在 {timeout}s 内未就绪：{error}',
    # main.py:514
    'ready: the daemon answered /readyz': '就绪：守护进程已响应 /readyz',
    # main.py:524
    (
        'Show the daemon and library versions: service, octop-gateway and Python (`GET '
        '/v1/version`). `--json` prints the raw version object.'
    ): (
        '显示守护进程与依赖库版本：服务、octop-gateway 和 Python（`GET /v1/version`）。`--json` '
        '打印原始版本对象。'
    ),
    # main.py:528
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw version\n'
        '$ imgw version --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw version\n'
        '$ imgw version --json\n'
        ''
    ),
    # main.py:535
    'Show daemon, octop-gateway and Python versions.': '显示守护进程、octop-gateway 和 Python 版本。',
    # main.py:564
    (
        'Show daemon status: service and octop-gateway versions, uptime, database and media '
        'paths, the effective backend URL, and per-channel state (`GET /v1/status`).'
    ): (
        '查看守护进程状态：服务与 octop-gateway '
        '版本、运行时长、数据库与媒体路径、生效的后端 URL，以及每个通道的状态（`GET '
        '/v1/status`）。'
    ),
    # main.py:569
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw status\n'
        '$ imgw status --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw status\n'
        '$ imgw status --json\n'
        ''
    ),
    # main.py:576
    'Show daemon status and per-channel state.': '查看守护进程状态与每个通道状态。',
    # main.py:637
    'not ready: {error}': '未就绪：{error}',
    # main.py:640
    'ready: {state}': '就绪：{state}',
    # main.py:615
    (
        "Probe the daemon's readiness endpoint (`GET /readyz`). Exit code 0 means ready; an "
        'unreachable or not-ready daemon exits 1, which makes this command usable in scripts and '
        'behind `imgw start --wait`.'
    ): (
        '探测守护进程的就绪端点（`GET /readyz`）。退出码 0 '
        '表示就绪；守护进程不可达或未就绪时退出码为 1，因此该命令可直接用于脚本，也被 `imgw start'
        ' '
        '--wait` 使用。'
    ),
    # main.py:620
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw ready\n'
        "$ imgw ready && imgw send sales ou_xxx 'the daemon is up'\n"
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw ready\n'
        "$ imgw ready && imgw send sales ou_xxx 'the daemon is up'\n"
        ''
    ),
    # main.py:627
    'Exit 0 when the daemon is ready.': '守护进程就绪时退出码为 0。',
    # main.py:666
    'channel_id or alias of the channel to send through.': '要发送的通道 channel_id 或 alias。',
    # main.py:670
    'Target subject_id (omit with --all).': '目标 subject_id（使用 --all 时省略）。',
    # main.py:672
    'Message text (plain text).': '消息文本（纯文本）。',
    # main.py:675
    'Broadcast to every known subject of the channel.': '广播给该通道所有已知 subject。',
    # main.py:686
    (
        'missing TEXT: usage is `imgw send <ref> --all <text>`'
    ): (
        '缺少 TEXT：用法为 `imgw send <ref> --all <text>`'
    ),
    # main.py:704
    (
        'usage is `imgw send <ref> <subject_id> <text>` or `imgw send <ref> --all <text>`'
    ): (
        '用法为 `imgw send <ref> <subject_id> <text>` 或 `imgw send <ref> --all <text>`'
    ),
    # main.py:644
    (
        'Push one text message to a subject (`POST /v1/messages`), or broadcast to every known '
        'subject of the channel with `--all` (`POST /v1/messages/broadcast`). A fresh '
        'Idempotency-Key is generated for every single-subject send. The confirmation line is '
        'always English for pipe stability: `sent to <channel>/<subject>`.'
    ): (
        '向一个 subject 推送一条文本消息（`POST /v1/messages`），或用 `--all` '
        '广播给该通道所有已知 subject（`POST /v1/messages/broadcast`）。每次单 subject '
        '发送都会生成新的 Idempotency-Key。为保持管道稳定，确认行始终为英文：`sent to '
        '<channel>/<subject>`。'
    ),
    # main.py:651
    (
        'Examples:\n'
        '\n'
        '\b\n'
        "$ imgw send sales ou_xxx 'hello'\n"
        "$ imgw send sales --all 'maintenance at 22:00'\n"
        "$ imgw send feishu:acme:sales ou_xxx 'hello' --json\n"
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        "$ imgw send sales ou_xxx 'hello'\n"
        "$ imgw send sales --all 'maintenance at 22:00'\n"
        "$ imgw send feishu:acme:sales ou_xxx 'hello' --json\n"
        ''
    ),
    # main.py:659
    'Push a message to one subject or broadcast.': '向单个 subject 推送消息或广播。',
    # main.py:744
    'Filter by channel_id or alias.': '按 channel_id 或 alias 过滤。',
    # main.py:746
    'Filter by subject_id.': '按 subject_id 过滤。',
    # main.py:749
    'Only messages at/after this epoch timestamp (seconds).': '只返回该 epoch 时间戳（秒）及之后的消息。',
    # main.py:751
    'Maximum number of messages to return.': '最多返回的消息条数。',
    # main.py:774
    '(no messages)': '（无消息）',
    # main.py:724
    (
        "Query the daemon's stored message history (`GET /v1/history`, read-only). Both inbound "
        'and outbound messages are shown with timestamps, direction and a truncated text preview.'
    ): (
        '查询守护进程保存的消息历史（`GET '
        '/v1/history`，只读）。同时显示入站与出站消息，包含时间戳、方向和截断后的文本预览。'
    ),
    # main.py:729
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw history --limit 20\n'
        '$ imgw history --channel sales --subject ou_xxx --since 1730000000\n'
        '$ imgw history --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw history --limit 20\n'
        '$ imgw history --channel sales --subject ou_xxx --since 1730000000\n'
        '$ imgw history --json\n'
        ''
    ),
    # main.py:737
    'Query stored message history (read-only).': '查询已保存的消息历史（只读）。',
    # main.py:811
    'Only events for this channel_id or alias.': '只显示该 channel_id 或 alias 的事件。',
    # main.py:831
    (
        'cannot reach the im-gateway daemon: {error}. Start it with `imgw run`.'
    ): (
        '无法连接 im-gateway 守护进程：{error}。请用 `imgw run` 启动它。'
    ),
    # main.py:792
    (
        "Tail the daemon's SSE event stream (`GET /v1/events`) until Ctrl-C or the connection "
        'closes. Each stdout line is one JSON event; keepalive comments are dropped. Use '
        '`--channel` to filter.'
    ): (
        '持续 tail 守护进程的 SSE 事件流（`GET /v1/events`），直到 Ctrl-C 或连接关闭。stdout '
        '每行是一个 JSON 事件；keepalive 注释行会被丢弃。可用 `--channel` 过滤。'
    ),
    # main.py:797
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw events\n'
        '$ imgw events --channel sales\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw events\n'
        '$ imgw events --channel sales\n'
        ''
    ),
    # main.py:804
    'Tail the SSE event stream.': 'tail SSE 事件流。',
    # main.py:897
    'all checks passed': '所有检查通过',
    # main.py:897
    'doctor found problems': 'doctor 发现问题',
    # main.py:852
    (
        'Run control-plane self-checks: `/healthz`, `/readyz`, `/v1/version` and `/v1/status` are'
        ' '
        'called in order and every result is printed. Exit code 0 when all checks pass, 1 '
        'otherwise.'
    ): (
        '运行控制面自检：依次调用 `/healthz`、`/readyz`、`/v1/version` 和 `/v1/status` '
        '并打印每项结果。全部通过时退出码为 0，否则为 1。'
    ),
    # main.py:857
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw doctor\n'
        '$ imgw doctor --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw doctor\n'
        '$ imgw doctor --json\n'
        ''
    ),
    # main.py:864
    'Run control-plane self-checks.': '运行控制面自检。',
    # main.py:932
    'Control-plane listen host.': '控制面监听地址。',
    # main.py:933
    'Control-plane listen port.': '控制面监听端口。',
    # main.py:914
    (
        'Start the gateway daemon in the foreground (the production entry point). This is the '
        'only command that starts a process: it migrates and opens the SQLite database, '
        'reconciles enabled channels from the database, and serves the control plane plus all '
        'channel connections in one asyncio loop. Every inbound turn is POSTed to one backend '
        'URL: `--backend-url`, then `$IMGW_BACKEND_URL`, then `http://127.0.0.1:8080`. Stop it '
        'with Ctrl-C; for a managed service use `imgw install-service` followed by `imgw start`.'
    ): (
        '在前台启动网关守护进程（生产入口）。这是唯一会启动进程的命令：它会迁移并打开 SQLite '
        '数据库，从数据库恢复已启用的通道，并在同一个 asyncio '
        '事件循环里提供控制面与全部通道连接。所有入站 turn 都 POST 到唯一后端 URL：'
        '`--backend-url`，其次 `$IMGW_BACKEND_URL`，最后 `http://127.0.0.1:8080`。用 Ctrl-C '
        '停止；作为系统服务托管请先用 `imgw install-service` 安装，再执行 `imgw start`。'
    ),
    # main.py:922
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw run\n'
        '$ imgw run --backend-url http://127.0.0.1:8080\n'
        '$ imgw run --host 127.0.0.1 --port 8765\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw run\n'
        '$ imgw run --backend-url http://127.0.0.1:8080\n'
        '$ imgw run --host 127.0.0.1 --port 8765\n'
        ''
    ),
    # main.py:929
    'Start the gateway daemon (production).': '启动网关守护进程（生产入口）。',
    # main.py:936
    (
        'Agent backend base URL; every turn is POSTed to `{url}/v1/turns` (default: '
        '$IMGW_BACKEND_URL, then http://127.0.0.1:8080).'
    ): (
        'agent 后端基础 URL；每个 turn 都会 POST 到 `{url}/v1/turns`（默认取 '
        '$IMGW_BACKEND_URL，其次 http://127.0.0.1:8080）。'
    ),
    # main.py:968
    'Loopback port for the in-process echo backend.': '进程内回显后端的回环端口。',
    # main.py:942
    (
        'Start the daemon in development mode. On top of a normal run, an in-process echo backend'
        ' '
        '(`im_gateway.dev.backend`) listens on `--dev-backend-port`, and the global backend URL is'
        ' '
        'pointed at it (all other mechanisms — token, timeout, history, dedup — are unchanged).'
        ' '
        'The echo backend replies to any inbound message with one `message` frame and one '
        '`completed` frame listing channel, subject, turn and idempotency ids, so channel '
        'connectivity can be verified without a real backend.'
    ): (
        '以开发模式启动守护进程。在普通运行的基础上，进程内回显后端（`im_gateway.dev.backend`）'
        '监听 '
        '`--dev-backend-port`，并把全局后端 URL 指向它（token、超时、历史、去重等其他机制均不'
        '变）。回显后端对任意入站消息回复一个 `message` 帧和一个 `completed` 帧，内容包含 '
        'channel、subject、turn 与 idempotency id，因此无需真实后端即可验证通道连通性。'
    ),
    # main.py:951
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw run-dev\n'
        '$ imgw run-dev --port 8765 --dev-backend-port 9900\n'
        '$ imgw run-dev --host 0.0.0.0 --port 9000\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw run-dev\n'
        '$ imgw run-dev --port 8765 --dev-backend-port 9900\n'
        '$ imgw run-dev --host 0.0.0.0 --port 9000\n'
        ''
    ),
    # main.py:959
    'Start the daemon with the in-process echo backend.': '以进程内回显后端启动守护进程。',
    # main.py:988
    'the gateway runtime is not available: {error}': '网关运行时不可用：{error}',
    # main.py:1021
    'Print the commands without writing files or running them.': '只打印命令，不写文件也不执行。',
    # main.py:1027
    (
        'Install a user-scoped service (default, no root) or a system-wide one (needs root).'
    ): (
        '安装用户级服务（默认，无需 root）或系统级服务（需要 root）。'
    ),
    # main.py:1037
    (
        'Working directory of the daemon (default: the current directory).'
    ): (
        '守护进程的工作目录（默认：当前目录）。'
    ),
    # main.py:1044
    (
        'Print the generated definition without writing or executing anything.'
    ): (
        '只打印生成的服务定义，不写入也不执行任何内容。'
    ),
    # main.py:998
    (
        'Generate the platform service definition (`systemd` unit, `launchd` plist or NSSM '
        'commands) and install/enable it so the OS service manager keeps `imgw run` alive. The '
        'default is a user-scoped service; `--system` installs system-wide (needs root). '
        '`--print-only` prints the definition without writing or executing anything; `--dry-run` '
        'prints the commands without running them.'
    ): (
        '生成平台服务定义（`systemd` unit、`launchd` plist 或 NSSM '
        '命令）并安装/启用，让操作系统服务管理器保持 `imgw run` '
        '存活。默认安装用户级服务；`--system` 安装系统级服务（需要 root）。`--print-only` '
        '只打印定义，不写文件也不执行；`--dry-run` 只打印命令，不执行。'
    ),
    # main.py:1006
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw install-service --print-only\n'
        '$ imgw install-service --dry-run\n'
        '$ imgw install-service --system --workdir /srv/im-gateway\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw install-service --print-only\n'
        '$ imgw install-service --dry-run\n'
        '$ imgw install-service --system --workdir /srv/im-gateway\n'
        ''
    ),
    # main.py:1014
    'Install the OS service definition.': '安装 OS 服务定义。',
    # main.py:1094
    'Print the commands without executing them.': '只打印命令，不执行。',
    # main.py:1100
    (
        'Manage the user-scoped service (default) or the system-wide one.'
    ): (
        '管理用户级服务（默认）或系统级服务。'
    ),
    # main.py:1074
    (
        'Stop and remove the platform service definition installed by `imgw install-service`. The'
        ' '
        'default is the user-scoped service; `--system` targets the system-wide one.'
    ): (
        '停止并移除由 `imgw install-service` 安装的平台服务定义。默认操作用户级服务；`--system` '
        '作用于系统级服务。'
    ),
    # main.py:1079
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw uninstall-service\n'
        '$ imgw uninstall-service --system\n'
        '$ imgw uninstall-service --dry-run\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw uninstall-service\n'
        '$ imgw uninstall-service --system\n'
        '$ imgw uninstall-service --dry-run\n'
        ''
    ),
    # main.py:1087
    'Remove the OS service definition.': '移除 OS 服务定义。',
    # main.py:1139
    'Poll /readyz until the daemon is ready.': '轮询 /readyz 直到守护进程就绪。',
    # main.py:1118
    (
        'Start the installed OS service through the platform service manager (systemd / launchd /'
        ' '
        'NSSM). `--wait` additionally polls `/readyz` for up to 30 seconds and fails if the '
        'daemon does not become ready. The default is the user-scoped service.'
    ): (
        '通过平台服务管理器（systemd / launchd / NSSM）启动已安装的系统服务。`--wait` 会额外轮询 '
        '`/readyz` 最多 30 秒，若守护进程未就绪则失败。默认操作用户级服务。'
    ),
    # main.py:1124
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw start\n'
        '$ imgw start --wait\n'
        '$ imgw start --system --dry-run\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw start\n'
        '$ imgw start --wait\n'
        '$ imgw start --system --dry-run\n'
        ''
    ),
    # main.py:1132
    'Start the installed OS service.': '启动已安装的 OS 服务。',
    # main.py:1168
    (
        'Stop the installed OS service through the platform service manager. The default is the '
        'user-scoped service; `--system` targets the system-wide one.'
    ): (
        '通过平台服务管理器停止已安装的系统服务。默认操作用户级服务；`--system` '
        '作用于系统级服务。'
    ),
    # main.py:1173
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw stop\n'
        '$ imgw stop --system\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw stop\n'
        '$ imgw stop --system\n'
        ''
    ),
    # main.py:1180
    'Stop the installed OS service.': '停止已安装的 OS 服务。',
    # main.py:1210
    (
        'Restart the installed OS service through the platform service manager. The default is '
        'the user-scoped service; `--system` targets the system-wide one.'
    ): (
        '通过平台服务管理器重启已安装的系统服务。默认操作用户级服务；`--system` '
        '作用于系统级服务。'
    ),
    # main.py:1215
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw restart\n'
        '$ imgw restart --system\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw restart\n'
        '$ imgw restart --system\n'
        ''
    ),
    # main.py:1222
    'Restart the installed OS service.': '重启已安装的 OS 服务。',
    # main.py:1321
    '--secret-stdin: no secret found on stdin': '--secret-stdin：没有从 stdin 读到 secret',
    # main.py:1378
    'the daemon did not return QR content; cannot write the PNG': '守护进程未返回二维码内容，无法写出 PNG',
    # main.py:1382
    'QR PNG: {path}': '二维码 PNG：{path}',
    # main.py:1405
    (
        '(this terminal cannot encode QR characters; printing the URL only -- use --qr-png for an'
        ' '
        'image)'
    ): (
        '（当前终端无法输出二维码字符，已改为只输出 URL —— 可用 --qr-png 生成图片）'
    ),
    # main.py:1416
    'the daemon did not return QR content': '守护进程未返回二维码内容',
    # main.py:1452
    'the daemon did not return a login session_id': '守护进程未返回登录 session_id',
    # main.py:1461
    (
        'session: {session_id} (resume with --session-id {session_id})'
    ): (
        '会话：{session_id}（可用 --session-id {session_id} 继续）'
    ),
    # main.py:1475
    'status: {status}': '状态：{status}',
    # main.py:1480
    'login confirmed: {ref}': '登录已确认：{ref}',
    # main.py:1483
    'warning: {message}': '警告：{message}',
    # main.py:1488
    'login timed out after {timeout:g}s': '登录在 {timeout:g}s 后超时',
    # main.py:1493
    'login {status}{reason}': '登录 {status}{reason}',
    # main.py:1501
    'logged in: {channel_id} ({state})': '已登录：{channel_id}（{state}）',
    # main.py:1628
    (
        'Existing channel_id/alias to re-bind, or a channel kind '
        '(feishu/wecom/dingtalk/qq/weixin) to create a new channel.'
    ): (
        '已存在的 channel_id/alias（用于重新绑定），或通道 '
        'kind（feishu/wecom/dingtalk/qq/weixin）用于新建通道。'
    ),
    # main.py:1638
    (
        'Immutable channel_id for a new channel (default: <kind>:<name>).'
    ): (
        '新通道的不可变 channel_id（默认：<kind>:<name>）。'
    ),
    # main.py:1645
    'Instance name for a new channel (also used as the CLI alias).': '新通道的实例名（同时用作 CLI alias）。',
    # main.py:1650
    (
        'Extra channel config as KEY=VALUE; repeatable (values are typed as bool/int/string).'
    ): (
        '额外的通道配置 KEY=VALUE；可重复（值会按 bool/int/string 自动推断）。'
    ),
    # main.py:1662
    'feishu/qq: app_id.': 'feishu/qq 的 app_id。',
    # main.py:1665
    (
        'feishu/qq/dingtalk: app_secret (QQ stores it as `secret`).'
    ): (
        'feishu/qq/dingtalk：app_secret（QQ 会把 secret 存为 `secret`）。'
    ),
    # main.py:1667
    'wecom: bot_id.': 'wecom 的 bot_id。',
    # main.py:1668
    'wecom/qq: bot or application secret.': 'wecom/qq：机器人或应用 secret。',
    # main.py:1669
    'dingtalk: app_key (client_id).': 'dingtalk 的 app_key（client_id）。',
    # main.py:1672
    'qq: bot token, or weixin: account bot token.': 'qq：机器人 token；或 weixin：账号 bot token。',
    # main.py:1676
    'weixin: account id (also fills the `accounts` entry).': 'weixin：账号 id（同时填充 `accounts` 条目）。',
    # main.py:1678
    'weixin: API base URL.': 'weixin 的 API base URL。',
    # main.py:1681
    'Read the secret from stdin instead of argv.': '从 stdin 读取 secret，而不是放进 argv。',
    # main.py:1685
    'Force a fresh QR scan even when credentials exist.': '即使已有凭据也强制重新扫码。',
    # main.py:1689
    'Probe credentials before registering a new channel.': '注册新通道前先探测凭据。',
    # main.py:1695
    (
        'QR output: both (URL + terminal QR), url (URL only), qr (terminal QR only) or json '
        '(NDJSON events).'
    ): (
        '二维码输出方式：both（URL + 终端二维码）、url（仅 URL）、qr（仅终端二维码）或 '
        'json（NDJSON 事件）。'
    ),
    # main.py:1703
    'Alias for --format json (NDJSON events).': '--format json 的别名（NDJSON 事件）。',
    # main.py:1707
    'Also write the QR code to this PNG path.': '同时把二维码写入该 PNG 路径。',
    # main.py:1711
    (
        'Overall login wait in seconds (default 15 minutes).  Also the QR validity '
        'where the platform honours it; platforms with their own TTL report it and '
        'the wait is clamped to that value.'
    ): (
        '登录等待总时长（秒，默认 15 分钟）。平台支持时也作为二维码有效期；'
        '平台自带 TTL 时会回传，等待时长会被限制在其范围内。'
    ),
    # main.py: login hint (app / validity / fallback link)
    'Scan the QR code with {app}, or open this link in {app}: {url}': (
        '用{app}扫描二维码，或者在{app}中点击链接：{url}'
    ),
    'Scan the QR code with {app} (valid for {ttl}), or open this link in {app}: {url}': (
        '用{app}扫描二维码，二维码有效期 {ttl}，或者在{app}中点击链接：{url}'
    ),
    '{value} seconds': '{value} 秒',
    '{value} minutes': '{value} 分钟',
    '1 second': '1 秒',
    '1 minute': '1 分钟',
    '{minutes} minutes {seconds} seconds': '{minutes} 分 {seconds} 秒',
    'this app': '对应的 App',
    'WeChat': '微信',
    'WeCom': '企业微信',
    'DingTalk': '钉钉',
    'Feishu': '飞书',
    'QQ': '手机QQ',
    # main.py:1715
    'Poll interval while waiting for confirmation (seconds).': '等待确认时的轮询间隔（秒）。',
    # main.py:1721
    (
        'Print the challenge and exit; resume with --session-id.'
    ): (
        '打印 challenge 后立即退出；之后用 --session-id 继续。'
    ),
    # main.py:1728
    'Resume an existing login session for the same channel ref.': '继续同一通道已有的登录会话。',
    # main.py:1744
    'unsupported --format {value!r}: choose {choices}': '不支持的 --format {value!r}：可选 {choices}',
    # main.py:1751
    '--timeout must be a positive number': '--timeout 必须为正数',
    # main.py:1769
    '--session-id can only be used with an existing channel': '--session-id 只能用于已存在的通道',
    # main.py:1798
    (
        'created {channel_id} (disabled until the QR login completes)'
    ): (
        '已创建 {channel_id}（在扫码登录完成前处于停用状态）'
    ),
    # main.py:1833
    (
        'channel {ref!r} ({kind}) does not support QR login; pass credentials instead '
        '(--app-id/--app-secret, ...)'
    ): (
        '通道 {ref!r}（{kind}）不支持扫码登录；请改用凭据参数（--app-id/--app-secret 等）直接登录'
    ),
    # main.py:1563
    (
        'Bind or re-bind a channel instance: scan a QR code, pass credentials silently, or '
        'reconnect an already-bound channel.\n'
        '\n'
        'TARGET is either an existing channel_id/alias to re-bind, or a channel kind '
        '(feishu/wecom/dingtalk/qq/weixin) to create a new instance.\n'
        '\n'
        'Behavior matrix:\n'
        '\n'
        '\b\n'
        'new target, no credentials         QR login (auto-enabled on confirmation)\n'
        'new target, credential flags       silent registration (no QR, enabled immediately)\n'
        'existing target, credentials ok    reconnect / re-enable without scanning\n'
        'existing target, credentials lost  QR login\n'
        'existing target, credential flags  overwrite credentials, then reconnect\n'
        'existing target + --rescan         force a fresh QR scan (expired token, new account)\n'
        '\n'
        'Per-platform credential flags:\n'
        '\n'
        '\b\n'
        'feishu    --app-id / --app-secret\n'
        'wecom     --bot-id / --secret\n'
        'dingtalk  --app-key / --app-secret\n'
        'qq        --app-id / --secret        (QQ stores the secret as `secret`)\n'
        'weixin    --token / --account-id / --base-url   (also builds an `accounts` entry)\n'
        '\n'
        'Use --secret-stdin to read the secret from stdin instead of argv (keeps it out of the '
        'process list), --check to probe new credentials before registering, and --no-wait to '
        'print the QR challenge immediately and resume later with --session-id.'
    ): (
        '绑定或重新绑定一个通道实例：扫码登录、静默传入凭据，或重连已绑定的通道。\n'
        '\n'
        'TARGET 可以是已存在的 channel_id/alias（重新绑定），也可以是通道 '
        'kind（feishu/wecom/dingtalk/qq/weixin，用于新建实例）。\n'
        '\n'
        '行为矩阵：\n'
        '\n'
        '\b\n'
        '新目标，无凭据        扫码登录（确认后自动启用）\n'
        '新目标，带凭据参数    静默注册（不扫码，立即启用）\n'
        '已有目标，凭据齐全    直接重连 / 重新启用，不扫码\n'
        '已有目标，凭据缺失    扫码登录\n'
        '已有目标，带凭据参数  覆盖凭据后重连\n'
        '已有目标 + --rescan   强制重新扫码（token 过期、换账号）\n'
        '\n'
        '各平台凭据参数：\n'
        '\n'
        '\b\n'
        'feishu    --app-id / --app-secret\n'
        'wecom     --bot-id / --secret\n'
        'dingtalk  --app-key / --app-secret\n'
        'qq        --app-id / --secret        （QQ 把 secret 存为 `secret`）\n'
        'weixin    --token / --account-id / --base-url   （同时生成 `accounts` 条目）\n'
        '\n'
        '用 --secret-stdin 从 stdin 读取 secret 而不是放在 argv 里（避免出现在进程列表），用 '
        '--check 在注册前先探测新凭据，用 --no-wait 立即打印二维码 challenge 并稍后用 '
        '--session-id 继续。'
    ),
    # main.py:1594
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '# 1. create a channel by QR scan (prints URL + terminal QR; also writes a PNG)\n'
        '$ imgw channel login weixin:wx --qr-png ./weixin-qr.png\n'
        '\n'
        '\b\n'
        '# 2. create a channel silently from credentials (no scan)\n'
        '$ imgw channel login feishu --id feishu:acme:sales --name sales \\\n'
        '    --app-id cli_xxx --app-secret "$FEISHU_APP_SECRET"\n'
        '\n'
        '\b\n'
        '# 3. overwrite the credentials of an existing channel and reconnect\n'
        '$ imgw channel login feishu:acme:sales --app-id cli_yyy \\\n'
        '    --app-secret "$FEISHU_APP_SECRET"\n'
        '\n'
        '\b\n'
        '# 4. force a fresh scan (e.g. an expired WeChat session)\n'
        '$ imgw channel login weixin:wx --rescan\n'
        '\n'
        '\b\n'
        '# 5. scripted flow: print the URL and exit, then resume with --session-id\n'
        '$ imgw channel login weixin:wx --format url --no-wait\n'
        '$ imgw channel login weixin:wx --session-id abc123 --format json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '# 1. 扫码创建通道（打印 URL + 终端二维码，同时写出 PNG）\n'
        '$ imgw channel login weixin:wx --qr-png ./weixin-qr.png\n'
        '\n'
        '\b\n'
        '# 2. 用凭据静默创建通道（不扫码）\n'
        '$ imgw channel login feishu --id feishu:acme:sales --name sales \\\n'
        '    --app-id cli_xxx --app-secret "$FEISHU_APP_SECRET"\n'
        '\n'
        '\b\n'
        '# 3. 覆盖已有通道的凭据并重连\n'
        '$ imgw channel login feishu:acme:sales --app-id cli_yyy \\\n'
        '    --app-secret "$FEISHU_APP_SECRET"\n'
        '\n'
        '\b\n'
        '# 4. 强制重新扫码（例如微信会话过期）\n'
        '$ imgw channel login weixin:wx --rescan\n'
        '\n'
        '\b\n'
        '# 5. 脚本流程：只打印 URL 后退出，之后用 --session-id 继续\n'
        '$ imgw channel login weixin:wx --format url --no-wait\n'
        '$ imgw channel login weixin:wx --session-id abc123 --format json\n'
        ''
    ),
    # main.py:1620
    'Bind or re-bind a channel (QR, credentials, reconnect).': '绑定或重新绑定通道（扫码/凭据/重连）。',
    # main.py:1883
    'channel_id or alias.': 'channel_id 或 alias。',
    # main.py:1902
    (
        'logged out: {channel_id} (disabled; credentials and history kept)'
    ): (
        '已登出：{channel_id}（已停用；凭据与历史保留）'
    ),
    # main.py:1865
    (
        'Log a channel out: disable it while keeping its credentials, subjects, message history '
        'and config. This is the old `disable` semantic and is idempotent. Re-bind with `imgw '
        'channel login <ref>` (no QR scan is needed while credentials remain).'
    ): (
        '登出通道：停用通道，同时保留凭据、subjects、消息历史与配置。这是旧的 `disable` '
        '语义，且幂等。凭据仍在时可用 `imgw channel login <ref>` 直接重连（无需重新扫码）。'
    ),
    # main.py:1871
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw channel logout sales\n'
        '$ imgw channel logout feishu:acme:sales --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw channel logout sales\n'
        '$ imgw channel logout feishu:acme:sales --json\n'
        ''
    ),
    # main.py:1878
    'Disable a channel, keeping its data.': '停用通道，保留其数据。',
    # main.py:1939
    (
        'Optional channel_id/alias to show one channel in detail.'
    ): (
        '可选的 channel_id/alias，用于查看单个通道详情。'
    ),
    # main.py:1943
    'Emit JSON (machine-readable) instead of TSV.': '输出 JSON（机器可读）而不是 TSV。',
    # main.py:1949
    (
        'Print the TSV header line (default: on; use --no-header for bare rows).'
    ): (
        '打印 TSV 表头行（默认开启；管道场景可用 --no-header 关闭）。'
    ),
    # main.py:1984
    (
        '(no channels; bind one with `imgw channel login <kind>`)'
    ): (
        '（没有通道；可用 `imgw channel login <kind>` 绑定一个）'
    ),
    # main.py:1911
    (
        'List channels, or show one channel in detail.\n'
        '\n'
        'Without REF: one TSV row per channel with the fixed columns `channel_id  kind  state  '
        'creds  alias` (a header is printed by default; `--no-header` prints bare rows for '
        'pipes). Disabled channels are included.\n'
        '\n'
        'With REF: a `key<TAB>value` detail view, including login support, credential status and '
        'the masked config (`app_secret` / `token` are always `***`; `${ENV}` references are '
        'preserved). Secrets never leave the daemon.'
    ): (
        '列出通道，或查看单个通道详情。\n'
        '\n'
        '不带 REF：每个通道一行 TSV，固定列为 `channel_id  kind  state  creds  '
        'alias`（默认打印表头；`--no-header` 输出裸数据行，便于管道处理）。disabled '
        '通道也会列出。\n'
        '\n'
        '带 REF：`key<TAB>value` 详情视图，包含是否支持登录、凭据状态，以及打码后的 '
        'config（`app_secret` / `token` 恒为 `***`；`${ENV}` 引用原样保留）。secret '
        '永远不会离开守护进程。'
    ),
    # main.py:1923
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw channel list\n'
        '$ imgw channel list | awk -F\'\\t\' \'$3 == "running" {print $1}\'\n'
        '$ imgw channel list --no-header | cut -f1\n'
        '$ imgw channel list sales\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw channel list\n'
        '$ imgw channel list | awk -F\'\\t\' \'$3 == "running" {print $1}\'\n'
        '$ imgw channel list --no-header | cut -f1\n'
        '$ imgw channel list sales\n'
        ''
    ),
    # main.py:1932
    'List channels or show one in detail.': '列出通道或查看单个通道详情。',
    # main.py:2013
    "Also delete the channel's subjects and messages.": '同时删除该通道的 subjects 与 messages。',
    # main.py:2017
    'Skip the interactive confirmation.': '跳过交互式确认。',
    # main.py:2028
    (
        'refusing to delete without confirmation; re-run with --yes (stdin is not a TTY)'
    ): (
        '未确认，拒绝删除；请加 --yes 重新执行（stdin 不是 TTY）'
    ),
    # main.py:2033
    'delete channel {ref!r}?': '删除通道 {ref!r}？',
    # main.py:2038
    'unknown channel: {ref!r}': '未知通道：{ref!r}',
    # main.py:2041
    (
        'channel {channel_id!r} is still logged in; run `imgw channel logout {channel_id}` first'
    ): (
        '通道 {channel_id!r} 仍处于登录状态；请先执行 `imgw channel logout {channel_id}`'
    ),
    # main.py:1989
    (
        'Delete a channel row (`DELETE /v1/channels/{ref}`). The channel must be logged out '
        'first: the daemon rejects deletion of an enabled channel with HTTP 409, and the CLI '
        'checks before calling. By default `subjects` and `messages` are kept; `--purge` deletes '
        'them too. `--yes` skips the interactive confirmation (required when stdin is not a TTY).'
    ): (
        '删除通道行（`DELETE /v1/channels/{ref}`）。必须先登出该通道：守护进程会以 HTTP 409 '
        '拒绝删除启用中的通道，CLI 也会在调用前预检。默认保留 `subjects` 与 `messages`；`--purge`'
        ' '
        '会一并删除。`--yes` 跳过交互式确认（stdin 不是 TTY 时必须加）。'
    ),
    # main.py:1996
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw channel logout sales\n'
        '$ imgw channel delete sales --yes\n'
        '$ imgw channel delete sales --yes --purge\n'
        '$ imgw channel delete sales --yes --json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw channel logout sales\n'
        '$ imgw channel delete sales --yes\n'
        '$ imgw channel delete sales --yes --purge\n'
        '$ imgw channel delete sales --yes --json\n'
        ''
    ),
    # main.py:2005
    'Delete a channel (log it out first).': '删除通道（需先登出）。',
    # main.py:2080
    'Channel kind to probe, e.g. feishu.': '要探测的通道 kind，例如 feishu。',
    # main.py:2085
    'Channel config as KEY=VALUE; repeatable.': '通道配置 KEY=VALUE；可重复。',
    # main.py:2062
    (
        'Probe channel credentials without registering a channel: the daemon starts a temporary '
        'instance, validates the credentials and tears it down (`POST /v1/channels/probe`). Use '
        'it before a silent `channel login` or when debugging bad credentials.'
    ): (
        '在不注册通道的情况下探测通道凭据：守护进程会启动临时实例、校验凭据后销毁（`POST '
        '/v1/channels/probe`）。适合在静默 `channel login` 之前或排查坏凭据时使用。'
    ),
    # main.py:2068
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw channel test feishu --config app_id=cli_xxx --config '
        'app_secret="$FEISHU_APP_SECRET"\n'
        '$ imgw channel test wecom --config bot_id="$WECOM_BOT_ID" --config '
        'secret="$WECOM_SECRET"\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw channel test feishu --config app_id=cli_xxx --config '
        'app_secret="$FEISHU_APP_SECRET"\n'
        '$ imgw channel test wecom --config bot_id="$WECOM_BOT_ID" --config '
        'secret="$WECOM_SECRET"\n'
        ''
    ),
    # main.py:2075
    'Probe credentials without registering.': '探测凭据，不注册通道。',
    # main.py:2150
    '(no known subjects yet)': '（暂无已知 subject）',
    # main.py:2116
    (
        'List the known push targets (subjects) of a channel (`GET /v1/channels/{ref}/subjects`).'
        ' '
        'Subjects are learned from inbound messages; each row shows subject_id, chat_type, '
        'display_name, first_seen and last_seen.'
    ): (
        '列出通道已知的推送目标（subjects）（`GET /v1/channels/{ref}/subjects`）。subjects '
        '来自入站消息；每行显示 subject_id、chat_type、display_name、first_seen 与 last_seen。'
    ),
    # main.py:2129
    "List a channel's known push targets.": '列出通道已知的推送目标。',
    # main.py:2194
    'Write the document to this file instead of stdout.': '把文档写入该文件而不是 stdout。',
    # main.py:2198
    'Include literal secrets instead of masking them.': '导出字面 secret，而不是打码。',
    # main.py:2214
    'cannot write {path}: {error}': '无法写入 {path}：{error}',
    # main.py:2219
    'exported config to {path}': '配置已导出到 {path}',
    # main.py:2172
    (
        'Export the portable config document as JSON (`GET /v1/config/export`). Secrets are '
        'masked by default: literal secrets become `***`, while `${ENV}` references stay as-is. '
        '`--with-secrets` includes literal secrets (handle the output carefully); `-o/--output` '
        'writes a file instead of stdout.'
    ): (
        '以 JSON 导出可移植配置文档（`GET /v1/config/export`）。默认对 secret 打码：字面 secret '
        '变为 `***`，`${ENV}` 引用保持原样。`--with-secrets` 会包含字面 '
        'secret（请谨慎处理输出）；`-o/--output` 写入文件而不是 stdout。'
    ),
    # main.py:2179
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw config export\n'
        '$ imgw config export -o cfg.json\n'
        '$ imgw config export --with-secrets -o cfg.secret.json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw config export\n'
        '$ imgw config export -o cfg.json\n'
        '$ imgw config export --with-secrets -o cfg.secret.json\n'
        ''
    ),
    # main.py:2187
    'Export the config document (secrets masked).': '导出配置文档（secret 打码）。',
    # main.py:2243
    'Config document to import.': '要导入的配置文档。',
    # main.py:2251
    'invalid config document {path}: {error}': '配置文档无效 {path}：{error}',
    # main.py:2224
    (
        'Validate and import a config document (`POST /v1/config/import`) as a single daemon-side'
        ' '
        'transaction: an invalid document changes nothing. Masked values (`***`) in the file mean'
        ' '
        "'keep the current value'."
    ): (
        '校验并导入配置文档（`POST '
        '/v1/config/import`），在守护进程侧作为单个事务执行：文档无效时不会产生任何变更。文件中的'
        '打码值（`***`）表示“保留当前值”。'
    ),
    # main.py:2229
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw config import cfg.json\n'
        '$ imgw --json config import cfg.json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw config import cfg.json\n'
        '$ imgw --json config import cfg.json\n'
        ''
    ),
    # main.py:2236
    'Import a config document in one transaction.': '在一个事务中导入配置文档。',
    # main.py:2295
    'Config document to validate.': '要校验的配置文档。',
    # main.py:2309
    (
        'config OK: {channels} channel(s), {settings} setting(s)'
    ): (
        'config OK：{channels} 个通道、{settings} 项设置'
    ),
    # main.py:2279
    (
        'Validate a config document locally against the pydantic schema; no daemon is required. '
        'Useful in CI and pre-commit hooks.'
    ): (
        '在本地按 pydantic schema 校验配置文档，无需守护进程。适合在 CI 与 pre-commit 中使用。'
    ),
    # main.py:2283
    (
        'Examples:\n'
        '\n'
        '\b\n'
        '$ imgw config validate cfg.json\n'
        '$ imgw config export -o cfg.json && imgw config validate cfg.json\n'
        ''
    ): (
        '示例：\n'
        '\n'
        '\b\n'
        '$ imgw config validate cfg.json\n'
        '$ imgw config export -o cfg.json && imgw config validate cfg.json\n'
        ''
    ),
    # main.py:2290
    'Validate a config document locally.': '在本地校验配置文档。',

    # --- src/im_gateway/cli/service.py ---
    # service.py:92
    '`{command}` failed with exit code {code}': '`{command}` 执行失败，退出码 {code}',
    # service.py:98
    '{message}: {stderr}': '{message}：{stderr}',
    # service.py:341
    'cannot write the service definition under {target}: {error}': '无法在 {target} 下写入服务定义：{error}',
    # service.py:458
    'cannot run `{command}`: {error}': '无法运行 `{command}`：{error}',
    # service.py:481
    'cannot remove {path}: {error}': '无法删除 {path}：{error}',
    # service.py:486
    (
        'unsupported platform {platform!r}: service management supports linux, darwin and win32'
    ): (
        '不支持的平台 {platform!r}：服务管理仅支持 linux、darwin 和 win32'
    ),
}

#: English catalog — identity mapping: the msgid *is* the English text.  The
#: key set deliberately matches the Chinese catalog so the catalogs stay in
#: parity (both are covered by ``tests/test_i18n.py``).
_EN_CATALOG: Final[dict[str, str]] = {msgid: msgid for msgid in _ZH_CATALOG}

CATALOGS: Final[dict[Language, dict[str, str]]] = {"en": _EN_CATALOG, "zh": _ZH_CATALOG}

#: Locale/environment variables consulted in priority order.  The typo
#: ``IMG_LANG`` is intentionally *not* one of them: only ``IMGW_LANG`` is the
#: explicit override.
_ENV_KEYS: Final[tuple[str, ...]] = ("IMGW_LANG", "LC_ALL", "LC_MESSAGES", "LANG")

#: Windows LANGID: primary language id 0x04 = Chinese, low 10 bits carry it.
_WINDOWS_LANG_CHINESE: Final[int] = 0x04
_WINDOWS_PRIMARY_LANG_MASK: Final[int] = 0x3FF

#: Language of this process; replaced by :func:`detect_language` at import time.
_language: Language = "en"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _normalize(raw: str) -> Language:
    """Map one locale/UI-language value to ``"zh"`` or ``"en"``.

    Recognises ``zh_CN.UTF-8`` / ``zh-CN`` / ``zh``, the Windows locale name
    ``Chinese (Simplified)_China``, the codepage form ``936`` / ``cp936`` and
    the code pages ``GBK`` / ``GB2312`` / ``GB18030`` as Chinese.  Everything
    else (``en_US.UTF-8``, ``C``, ``POSIX``, French, ...) is English.
    """
    token = raw.strip().lower().replace("_", "-")
    if not token:
        return "en"
    primary = token.split("-", 1)[0]
    if primary in {"zh", "chi", "zho"} or "chinese" in token:
        return "zh"
    if "936" in token or token in {"gbk", "gb2312", "gb18030", "ms936"}:
        return "zh"
    return "en"


def _windows_ui_language() -> str | None:
    """Return ``"zh"``/``"en"`` from the Windows UI language, else ``None``."""
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        langid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except (AttributeError, OSError, ValueError):
        return None
    if (langid & _WINDOWS_PRIMARY_LANG_MASK) == _WINDOWS_LANG_CHINESE:
        return "zh"
    return "en"


def _os_language() -> str:
    """Best-effort OS-localization probe when no locale variable is set."""
    windows_language = _windows_ui_language()
    if windows_language is not None:
        return windows_language
    try:
        language, _encoding = locale.getlocale()
    except (TypeError, ValueError):
        return ""
    return language or ""


def detect_language(env: Mapping[str, str] | None = None) -> Language:
    """Detect the CLI language from *env* (default: :data:`os.environ`).

    Priority: ``IMGW_LANG``, ``LC_ALL``, ``LC_MESSAGES``, ``LANG``, then the OS
    localization, then English.  The first non-empty variable decides; unknown
    values (e.g. a French locale) fall back to English rather than continuing
    down the list.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    for key in _ENV_KEYS:
        value = environ.get(key, "")
        if value.strip():
            return _normalize(value)
    return _normalize(_os_language())


# ---------------------------------------------------------------------------
# State and translation
# ---------------------------------------------------------------------------


def set_language(language: Language) -> None:
    """Force the language for the rest of this process."""
    global _language
    if language not in CATALOGS:
        raise ValueError(f"unsupported language {language!r}: expected 'en' or 'zh'")
    _language = language


def get_language() -> Language:
    """Return the active language (``"en"`` or ``"zh"``)."""
    return _language


def t(message_id: str, /, **kwargs: object) -> str:
    """Translate *message_id* into the active language and format it.

    English is the source language, so an unknown msgid falls back to the
    msgid itself.  ``str.format`` runs only when keyword arguments are given,
    which keeps literal braces in prose and examples safe.
    """
    template = CATALOGS[_language].get(message_id, message_id)
    return template.format(**kwargs) if kwargs else template


# Resolve once at import: Typer/Click help strings are built at import time.
_language = detect_language()
