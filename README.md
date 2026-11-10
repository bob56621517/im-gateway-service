# im-gateway-service

把 **octop-gateway**（MIT 的 Python 通道库）包成一层**单进程 IM 网关服务**：一个进程同时承载
控制面（CLI 用的 HTTP API）与所有 IM 通道连接；后端 agent 在进程外，通过 webhook 流式交互。

命令行的完整参考与命令变更记录见 [docs/CLI.md](docs/CLI.md)；设计与全部决策记录见 [HANDOVER.md](HANDOVER.md)（重点：§12 线上契约、§13 状态/存储/API、§14 决策定稿、§15 实施状态）。
当前开发与真机验证进度见 [docs/PROGRESS.md](docs/PROGRESS.md)。

## 当前状态

| 能力 | 状态 |
|---|---|
| 线上契约 `TurnFrame`（11 种帧 + 逐帧校验 + 上游互转 + metadata 清洗） | ✅ 单元测试覆盖 |
| SQLite（9 张表：配置 + 运行数据）/ Store / 迁移 / 备份 / retention 调度 | ✅ 单元测试覆盖 |
| FastAPI 控制面（healthz/readyz/status/channels/probe/config export-import/history/messages/maintenance/events/login） | ✅ 单元测试覆盖（含 `TestClient`） |
| 模型 A processor（`POST /v1/turns` + SSE → `MessageEvent`、幂等、subjects/messages 落库、超时/取消/错误映射） | ✅ 用 `httpx.MockTransport` 假后端完整单测 |
| CLI（17 个顶层命令，`channel`/`config` 含子命令）+ 系统服务生成与 start/stop/restart | ✅ 单元测试覆盖（fake runner） |
| 扫码/静默建号（`channel login`：飞书 / 企微 / 钉钉 / QQ / 微信 adapter 可注入；登录成功自动启用；`logout` 停用、`delete` 删行需先 logout；`list` secret 脱敏） | ✅ 状态机与 adapter 映射有单测；真实平台流程待真机验证 |
| dev-backend 调试套件（`im_gateway.dev.backend` + `imgw run-dev`） | ✅ 单测覆盖（TestClient + CLI 参数） |
| 真实平台连接与收发、capability 真机校准、outbox 重试 | ⏳ 需要真实凭证/网络，见 HANDOVER §15.3 |

**测试**：`uv run pytest` → 451 passed；`ruff`、`mypy`（strict）干净；`--cov=im_gateway` → 96%（未覆盖分支主要是 5 平台扫码 adapter 的真实网络调用，待真机冒烟覆盖）。
测试全部离线（MockTransport / fake manager / fake runner / TestClient），不依赖真实平台或网络。

## 快速开始

```bash
uv sync --all-groups           # 依赖（octop-gateway==1.0.3 等）
uv run imgw run                # 前台启动 daemon（唯一启动进程的命令）
uv run imgw status             # 另开终端；默认 tcp://127.0.0.1:8765
```

Windows 默认监听 `127.0.0.1:8765`；POSIX 默认 UDS `$XDG_RUNTIME_DIR/imgw.sock`。
控制面 token 首次启动自动生成（POSIX `~/.config/im-gateway/token`，Windows `%APPDATA%`），CLI 自动读取。

```bash
uv run imgw channel login feishu --name sales   # 扫码建号（飞书/企微/钉钉/QQ/微信）
uv run imgw channel login feishu --id feishu:acme:sales --name sales \
  --app-id cli_x --app-secret '${FEISHU_APP_SECRET}'   # 静默凭据（不扫码）
uv run imgw channel list                        # TSV，可 grep；--json / --header
uv run imgw channel list feishu:acme:sales      # 详情；config 中 secret 一律 ***
uv run imgw channel logout feishu:acme:sales    # 停用（保留凭据与历史）
uv run imgw channel delete feishu:acme:sales --yes   # 删除（须先 logout；--purge 级联数据）
uv run imgw config export -o cfg.json    # 默认脱敏（字面 secret -> "***"，${ENV} 保留）
uv run imgw config import cfg.json
uv run imgw send sales ou_xxx "hello"    # 主动推送（带 Idempotency-Key）
uv run imgw history --limit 20
uv run imgw doctor
```

开发调试（不需要真实后端）：

```bash
uv run imgw run-dev                  # 开发模式：内置回显后端，自动接管所有 tenant
uv run python -m im_gateway.dev.backend --port 9900   # 或独立跑调试套件
```

`imgw run-dev` 会在 `127.0.0.1:9900`（`--dev-backend-port` 可改）起一个回显后端（源码 `im_gateway.dev.backend`），把任何入站消息一次性回成包含 `channel / kind / tenant / subject_id / subject_name / chat_type / session_key / turn_id / idempotency_key / message` 的固定文本，便于验证网关 ↔ 通道连通性，无需写 webhook。

bootstrap 只有三项，全部可用环境变量/参数覆盖：`IMGW_DB`、`IMGW_LISTEN`、`IMGW_TOKEN_FILE`。
其余配置与运行数据都在 SQLite（`IMGW_DATA_DIR` 下的 `runtime.db`）。

作为系统服务：`imgw install-service`（默认用户级 systemd / launchd / NSSM，`--system` 需 root；`--print-only` 只生成）。
Windows/无 systemd 环境不支持时给出可读错误并 exit 1。

## 目录

```
src/im_gateway/
├── core/       contracts.py（TurnFrame）· config.py · capabilities.py · paths.py
├── db/         schema.py · models.py · store.py
├── gateway/    app.py（create_app/run_daemon）· runtime.py · factory.py · events.py
│               processor.py（模型 A）· idempotency.py · maintenance.py · login.py
└── cli/        main.py（typer）· client.py（httpx）· qr.py（segno）· service.py
tests/          契约 / 配置 / DB / 控制面 / processor / 登录 / service / CLI / 生命周期
```

## 开发门禁

```bash
uv run pytest -q
uv run pytest -q --cov=im_gateway --cov-report=term-missing
uv run ruff check src tests
uv run mypy
```

## 许可证

本项目 AGPL-3.0-or-later；依赖的 octop-gateway 为 MIT（分发时需附其许可证，见 HANDOVER §6）。
