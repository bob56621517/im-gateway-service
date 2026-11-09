# im-gateway-service

把 **octop-gateway**（MIT 的 Python 通道库）包成一层**单进程 IM 网关服务**：一个进程同时承载
控制面（CLI 用的 HTTP API）与所有 IM 通道连接；后端 agent 在进程外，通过 webhook 流式交互。

设计与全部决策记录见 [HANDOVER.md](HANDOVER.md)（重点：§12 线上契约、§13 状态/存储/API、§14 决策定稿、§15 实施状态）。

## 当前状态

| 能力 | 状态 |
|---|---|
| 线上契约 `TurnFrame`（11 种帧 + 逐帧校验 + 上游互转 + metadata 清洗） | ✅ 单元测试覆盖 |
| SQLite（9 张表：配置 + 运行数据）/ Store / 迁移 / 备份 / retention 调度 | ✅ 单元测试覆盖 |
| FastAPI 控制面（healthz/readyz/status/channels/probe/config export-import/history/messages/maintenance/events/login） | ✅ 单元测试覆盖（含 `TestClient`） |
| 模型 A processor（`POST /v1/turns` + SSE → `MessageEvent`、幂等、subjects/messages 落库、超时/取消/错误映射） | ✅ 用 `httpx.MockTransport` 假后端完整单测 |
| CLI（16 个顶层命令，`channel`/`config` 含子命令，含 `channel login`）/ 系统服务生成与 start/stop/restart | ✅ 单元测试覆盖（fake runner） |
| 扫码登录状态机（QQ / 微信 adapter 可注入，凭证合并写回 config） | ✅ 状态机与 adapter 映射有单测；真实平台流程待真机验证 |
| 真实平台连接与收发、capability 真机校准、outbox 重试 | ⏳ 需要真实凭证/网络，见 HANDOVER §15.3 |

**测试**：`uv run pytest` → 436 passed；`ruff`、`mypy`（strict）干净；`--cov=im_gateway` → 99%+。
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
uv run imgw channel add feishu --id feishu:acme:sales --tenant acme --alias sales \
  --config app_id=cli_x --config 'app_secret=${FEISHU_APP_SECRET}' --disabled
uv run imgw channel login sales          # QQ/微信：终端二维码扫码绑定
uv run imgw config export -o cfg.json    # 默认脱敏（字面 secret -> "***"，${ENV} 保留）
uv run imgw config import cfg.json
uv run imgw send sales ou_xxx "hello"    # 主动推送（带 Idempotency-Key）
uv run imgw history --limit 20
uv run imgw doctor
```

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
