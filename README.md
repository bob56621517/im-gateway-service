# im-gateway-service

把 **octop-gateway**（MIT 的 Python 通道库）包成一层**单进程 IM 网关服务**：一个 `imgw run` 进程同时承载
控制面 HTTP API 与所有 IM 通道连接；agent 后端在进程外，通过 webhook + SSE 流式交互。

> **用法文档就是帮助输出**：先 `uv run imgw --help`，再看 `uv run imgw <command> --help`（完整参数 + 可复制示例）。
> 帮助与错误文案支持中英双语（默认英文，系统区域为中文时自动中文；`IMGW_LANG=zh|en` 显式切换）。
> 破坏性变更/迁移历史见 [docs/CLI.md](docs/CLI.md)；设计定稿见 [HANDOVER.md](HANDOVER.md)；真机验证进度见 [docs/PROGRESS.md](docs/PROGRESS.md)。

## 架构

```text
 IM 平台 (飞书/企微/钉钉/QQ/微信)
      │  WebSocket / 回调 / 长轮询
      ▼
┌────────────────────── imgw run（单进程 daemon）──────────────────────┐
│  channel 适配器（octop-gateway）→ processor（模型 A）→ 后端 agent    │
│        │                              ▲                      │       │
│        │ 入站消息                      │ TurnFrame(SSE)        │ 回复  │
│        ▼                              │                      ▼       │
│  SQLite（配置/历史/审计）+ 控制面 HTTP API ←── CLI `imgw ...`（纯 client）│
└──────────────────────────────────────────────────────────────────────┘
```

- **模型 A**：入站消息 → `POST {backend}/v1/turns`（SSE 返回统一 `TurnFrame`）→ 帧转成通道回复，终态必为 `completed` / `error`；
- **多租户**：通道按 `tenant_id` 路由到不同后端；`imgw run-dev` 时所有 tenant 被内存覆盖到内置回显后端；
- **CLI 是纯 HTTP 客户端**：不直接碰数据库，所有写操作经控制面；secret 在视图中一律脱敏。

## 快速开始

```bash
uv sync --all-groups                     # 安装依赖（octop-gateway==1.0.3 等）
uv run imgw --help                       # 权威用法文档（每个子命令同样支持 --help）

uv run imgw run                          # 前台启动 daemon（唯一启动进程的命令）
# 另开终端：
uv run imgw status                       # 默认 tcp://127.0.0.1:8765；token 自动生成/读取
uv run imgw channel login feishu --name sales   # 扫码建号（飞书/企微/钉钉/QQ/微信）
uv run imgw channel list                 # TSV（--json / --header）
uv run imgw send sales ou_xxx "hello"    # 主动推送
uv run imgw history --limit 20           # 入站/出站记录
uv run imgw doctor                       # 自检
```

不接真实后端时用开发模式（内置回显后端，验证网关 ↔ 通道连通性）：

```bash
uv run imgw run-dev                      # 回显后端默认 127.0.0.1:9900（--dev-backend-port 可改）
```

系统服务：`uv run imgw install-service`（默认用户级 systemd / launchd / NSSM；`--system` 需 root；`--print-only` 只生成）。

## 目录

```text
src/im_gateway/
├── core/       contracts.py（TurnFrame）· config.py · capabilities.py · paths.py
├── db/         schema.py · models.py · store.py
├── gateway/    app.py（create_app/run_daemon）· runtime.py · factory.py · processor.py
│               login.py（5 平台扫码）· idempotency.py · maintenance.py · events.py
├── dev/        backend.py（回显后端，仅供 run-dev）
└── cli/        main.py（typer）· i18n.py · client.py · qr.py · service.py
tests/          契约 / 配置 / DB / 控制面 / processor / 登录 / i18n / CLI
```

## 开发门禁

```bash
uv run pytest -q                         # 513 passed
uv run ruff check src tests
uv run mypy src
```

## 许可证

本项目 AGPL-3.0-or-later；依赖的 octop-gateway 为 MIT（分发时需附其许可证，见 HANDOVER §6）。
