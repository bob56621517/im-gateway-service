# im-gateway-service

把 **octop-gateway**（MIT 的 Python 通道库）包成一层**单进程 IM 网关服务**：一个进程同时承载
控制面（CLI 用的 HTTP API）与所有 IM 通道连接；后端 agent 在进程外，通过 webhook 流式交互。

设计与全部决策记录见 [HANDOVER.md](HANDOVER.md)（重点：§12 线上契约、§13 状态/存储/API、§14 决策定稿）。

## 当前状态：milestone 1（控制面骨架）已完成

| 层 | 状态 |
|---|---|
| 线上契约 `TurnFrame`（11 种帧 + 逐帧校验 + 上游互转） | ✅ 已实现 + 测试 |
| SQLite（9 张表：配置 + 运行数据）/ Store / 迁移 | ✅ 已实现 + 测试 |
| FastAPI 控制面（healthz/readyz/status/channels/probe/config export-import/history/messages/maintenance/events） | ✅ 已实现 + 测试 |
| CLI（15 个命令，HTTP 客户端，不 import 通道栈） | ✅ 已实现 + 测试 |
| 真实后端 processor（模型 A：POST `/v1/turns` + SSE） | ⏳ milestone 2 |
| 扫码登录、systemd/NSSM service 命令、outbox 重试、retention 定时任务 | ⏳ milestone 2 |

真实 daemon 冒烟已通过：`imgw run` → `/healthz` `/readyz` → `status` / `channel add` /
`config export|import` / `restart` / `channel rm`。

## 快速开始

```bash
uv sync --all-groups           # 依赖（octop-gateway==1.0.3 等）
uv run imgw run                # 前台启动 daemon（唯一启动进程的命令）
uv run imgw status             # 另开终端；默认 tcp://127.0.0.1:8765
```

Windows 默认监听 `127.0.0.1:8765`；POSIX 默认 UDS `$XDG_RUNTIME_DIR/imgw.sock`。
控制面 token 首次启动自动生成（`~/.config/im-gateway/token`，Windows 在 `%APPDATA%`），CLI 自动读取。

```bash
uv run imgw channel add feishu --id feishu:acme:sales --tenant acme --alias sales \
  --config app_id=cli_x --config 'app_secret=${FEISHU_APP_SECRET}' --disabled
uv run imgw config export -o cfg.json     # 默认脱敏（字面 secret -> "***"，${ENV} 保留）
uv run imgw config import cfg.json
uv run imgw history --limit 20
uv run imgw doctor
```

bootstrap 只有三项，全部可用环境变量/参数覆盖：`IMGW_DB`、`IMGW_LISTEN`、`IMGW_TOKEN_FILE`。
其余配置与运行数据都在 SQLite（`IMGW_DATA_DIR` 下的 `runtime.db`）。

## 目录

```
src/im_gateway/
├── core/       contracts.py（TurnFrame）· config.py（导入导出文档）· capabilities.py · paths.py
├── db/         schema.py · models.py · store.py
├── gateway/    app.py（create_app/run_daemon）· runtime.py（Store+ChannelManager 接线）
│               factory.py（per-kind config）· events.py（SSE 总线）· dev_processor.py（占位 echo）
└── cli/        main.py（typer）· client.py（httpx）· qr.py（segno）
tests/          契约 / 配置 / DB / 控制面 / CLI / bootstrap 路径
```

## 开发门禁

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy
```

## 许可证

本项目 AGPL-3.0-or-later；依赖的 octop-gateway 为 MIT（分发时需附其许可证，见 HANDOVER §6）。
