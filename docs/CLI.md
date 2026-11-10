# CLI 参考与命令变更记录

本文是 `imgw` 的命令行契约：**每个命令做什么、参数怎么传、输出给谁（stdout/stderr）、旧命令迁移到哪个新命令**。
设计背景见 [HANDOVER.md](../HANDOVER.md) §13.6~§13.8。

---

## 1. 设计约定

1. **CLI 是纯 HTTP 客户端**：除 `run` / `run-dev` 外，所有命令只调 daemon 控制面（`--url` + `--token`），
   不直接读写 SQLite。
2. **stdout = 数据，stderr = 提示**：`| grep` / `| awk` 只会拿到数据行；进度、告警、错误一律走 stderr。
3. 全局参数：`--url`（或 `IMGW_URL`）、`--token`（或 `IMGW_TOKEN` / token 文件）、`--json`。
4. 退出码：成功 `0`；失败/超时/未就绪 `1`（参数错误由 typer 返回 `2`）。
5. 写操作都带审计（`audit` 表）；secret 不落日志、不进 `/v1/status`，`channel list` 视图里一律 `***`。

---

## 2. 命令总览

| 命令 | 作用 |
|---|---|
| `imgw run` | 前台启动 daemon（**唯一生产入口**） |
| `imgw run-dev` | 开发模式启动：内置回显后端接管所有 tenant |
| `imgw status` | daemon 状态 + 每通道状态 |
| `imgw ready` | 轮询 `/readyz`，就绪 exit 0 |
| `imgw channel login` | 绑定/重登通道（扫码 / 静默凭据 / 重连） |
| `imgw channel logout` | 停用通道（保留凭据与历史） |
| `imgw channel list` | 列通道（默认带表头 TSV）或看单通道详情 |
| `imgw channel delete` | 删除通道（**必须先 logout**） |
| `imgw channel test` | probe 凭据，不注册 |
| `imgw send` | 主动推送（单发 / `--all` 广播） |
| `imgw subject ls` | 通道已知推送目标 |
| `imgw history` | 聊天记录查询（in/out/error） |
| `imgw events` | tail 事件流（SSE） |
| `imgw doctor` | 自检（healthz/readyz/version/status…） |
| `imgw config export/import/validate` | 配置导入导出（默认脱敏）/本地校验 |
| `imgw db backup/vacuum/migrate` | 数据库维护 |
| `imgw install-service` / `uninstall-service` / `start` / `stop` / `restart` | 系统服务（NSSM/systemd） |
| `imgw version` | 版本 |

---

## 3. `channel` 子命令

### 3.1 `imgw channel login [TARGET]`

绑定或重绑一个通道。`TARGET` 可以是：

- **已存在的 `channel_id` / alias**：重登、换凭据、重新启用；
- **平台 kind**（`feishu` / `wecom` / `dingtalk` / `qq` / `weixin`）：创建新通道。

行为矩阵：

| TARGET | 是否给凭据参数 | 行为 |
|---|---|---|
| kind / 不存在 | 否 | 走扫码；成功后自动启用 |
| kind / 不存在 | 是 | 静默注册，不扫码；成功即启用 |
| 已存在，凭据齐全 | 否 | 直接重连/启用（不扫码） |
| 已存在，凭据缺失 | 否 | 走扫码 |
| 已存在 | 是 | 覆盖凭据后重连 |
| 已存在 + `--rescan` | — | 强制扫码（微信 token 过期、换账号时用） |

主要参数：

| 参数 | 说明 |
|---|---|
| `--id <channel_id>` | 新通道的不可变路由 id（默认 `<kind>:<--name>` 或 `<kind>:<随机>`） |
| `--name <name>` | 实例名（同时作为 alias） |
| `--tenant <tenant>` | 后端路由 tenant（默认 `""`） |
| `--config KEY=VALUE` | 通用配置，可重复（等价底层 `add`） |
| `--app-id` / `--app-secret` | 飞书 / QQ 凭据 |
| `--bot-id` / `--secret` | 企微凭据 |
| `--app-key` | 钉钉 app_key（client_id） |
| `--token` / `--account-id` / `--base-url` | 微信凭据（或 QQ token） |
| `--secret-stdin` | 从 stdin 读 secret，避免进程列表泄漏 |
| `--rescan` | 强制重新扫码 |
| `--check` | 新建前先 probe（仅静默模式） |
| `--format both\|url\|qr\|json` | 二维码输出形式，默认 `both` |
| `--json` | 等价 `--format json`（NDJSON） |
| `--qr-png <path>` | 另存 PNG（GUI/手机扫码用） |
| `--timeout` / `--interval` | 等待确认总时长 / 轮询间隔 |
| `--no-wait` | 只发起并打印 challenge 后退出 |
| `--session-id <id>` | 续轮询已有会话（同一 TARGET） |

示例：

```bash
# 扫码建号（窗口里会出 URL + 二维码；--qr-png 另存图片）
imgw channel login weixin:wx --qr-png .smoke/weixin-qr.png

# 静默注册（不扫码）
imgw channel login feishu --id feishu:smoke --name smoke \
  --app-id cli_xxx --app-secret '<secret>'

# 换凭据 / 重连
imgw channel login feishu:smoke --app-id cli_yyy --app-secret '<secret>'
imgw channel login weixin:wx --rescan          # 微信 session 过期后重扫

# 只给脚本用的输出
imgw channel login weixin:wx --format url --no-wait
imgw channel login weixin:wx --format json --qr-png .smoke/qr.png
```

### 3.2 `imgw channel logout <REF>`

**停用**通道（旧 `disable` 的语义），保留凭据、`subjects`、`messages`、历史；幂等。
之后可用 `imgw channel login <REF>` 直接重连（凭据还在，不需重扫）。

```bash
imgw channel logout weixin:wx
```

### 3.3 `imgw channel list [REF]`

- 无 REF：列全部通道（**默认带表头**），TSV 列固定为
  `channel_id  kind  state  creds  alias`；
- 带 REF：单通道详情，`key<TAB>value`，包含**脱敏**后的 `config.*`（`app_secret`/`token` 恒为 `***`）、
  `login_supported`、`credentials`。
- `--no-header`：纯数据行（管道用）；`--json`：结构化输出。
- 默认**包含 disabled 通道**（`state` 列区分）。

```bash
imgw channel list
imgw channel list | grep -E 'running|error'
imgw channel list | awk -F'\t' '$3=="running"{print $1}'
imgw channel list weixin:wx | grep '^config\.'
imgw channel list --no-header | cut -f1
```

### 3.4 `imgw channel delete <REF>`

删除通道行。**必须先把通道 logout**（否则服务端 409、CLI 也会预检报错），这是防止“正在运行的通道被直接删掉”的守卫。

- 默认只删行，保留 `subjects` / `messages`；
- `--purge` 级联删除 subjects/messages；
- `--yes` 跳过交互确认（非 TTY 时必需）。

```bash
imgw channel logout weixin:wx
imgw channel delete weixin:wx --yes
imgw channel delete weixin:wx --yes --purge
```

### 3.5 `imgw channel test <KIND>`

用 `--config` 给一组凭据做 probe（起临时实例验证后销毁），不注册通道。

---

## 4. `run` 与 `run-dev`

```bash
imgw run          # 生产入口：迁移 → 读 DB → reconcile channels → 起 uvicorn
imgw run-dev      # 开发模式：额外在 127.0.0.1:9900 起回显后端，所有 tenant 路由到它
```

`run-dev` 的用途：不写 webhook、不起外部后端，直接验证 **通道 ↔ 网关** 连通性。
它**不写 `backends` 表**，其余机制完全照旧：`messages` / `subjects` / `inbound_dedup` / `audit` 都正常落库。

收到任意入站消息后，回显后端返回一帧 `message` + 一帧 `completed`，文本形如：

```text
✅ dev-backend received a turn
channel_id      weixin:wx
channel_kind    weixin
tenant_id       -
subject_id      o9cq...@im.wechat
subject_name    -
chat_type       direct
session_key     ...
turn_id         ...
idempotency_key weixin:wx:752576...
message         你发的原文
```

回显后端也在源码里，可独立运行：

```bash
uv run python -m im_gateway.dev.backend --port 9900 [--token <t>]
```

端口用 `--dev-backend-port` 改。

---

## 5. 输出与管道约定

| 场景 | stdout | stderr |
|---|---|---|
| `channel list` | TSV 数据行（默认带表头） | `(no channels…)` 等提示 |
| `channel list <ref>` | `key<TAB>value` 行（含脱敏 config） | — |
| `channel login` 默认 | URL + 终端二维码 | 进度/状态/告警 |
| `channel login --format url` | **只有一行 URL** | 进度/状态/告警 |
| `channel login --format json` | NDJSON：`{"event":"challenge",…}` + `{"event":"result",…}` | 告警 |
| `logout` / `delete` | `--json` 时为结果 JSON，否则为空 | 操作结果提示 |

二维码相关：

- `--format both`（默认）：URL + 终端二维码；
- `--format qr`：只打印终端二维码；`--format url`：只打印 URL；
- 终端编码不支持二维码字符（如 Windows GBK 控制台）时自动降级为只打印 URL，并提示用 `--qr-png`；
- 微信 iLink 返回的是**落地页 URL**，CLI 会把它编码成二维码（不是下载图片）。

---

## 6. 命令变更记录（破坏性，已无旧别名）

### 6.1 命令映射

| 旧命令 | 新命令 | 说明 |
|---|---|---|
| `channel add <kind> --id --config` | `channel login <kind> --id --app-id …` | 静默注册统一走 login；`--config` 仍可用 |
| `channel ls` | `channel list` | 默认带表头；默认包含 disabled |
| `channel show <ref>` | `channel list <ref>` | 详情合并进 list（脱敏 config） |
| `channel rm <ref>` | `channel delete <ref>` | **必须先 logout** |
| `channel enable <ref>` | `channel login <ref>` | 凭据在就直接重连启用 |
| `channel disable <ref>` | `channel logout <ref>` | 语义不变（保留凭据/数据） |
| `run --dev-backend` | `run-dev` | 独立子命令 |

### 6.2 行为变化

1. `channel list` 默认**带表头**；需要裸数据用 `--no-header`；
2. `channel list` 默认**包含 disabled 通道**（`state` 列显示 `disabled`）；
3. `channel login` / `logout` / `list <ref>` 的 config 视图**强制脱敏**（secret → `***`，`${ENV}` 原样）；
4. 扫码登录成功会**自动启用**通道（不再是“登录完还是 disabled”）；
5. `delete` 前必须 `logout`（服务端 409 守卫）；
6. 支持 5 平台扫码：飞书 / 企微 / 钉钉 / QQ / 微信；
7. 空 backend token 时**不再发送**非法 `Authorization: Bearer ` 头；
8. 终端二维码编码失败自动降级 URL（Windows GBK）；
9. 修复微信二维码生成（把落地页 URL 编码成 QR，而不是下载网页）。

### 6.3 已移除

`channel add`、`channel ls`、`channel show`、`channel rm`、`channel enable`、`channel disable` 已删除，**不提供别名**。

---

## 7. 迁移示例

```bash
# 旧：手工建通道并禁用
imgw channel add feishu --id feishu:acme:sales --config app_id=cli_x --config app_secret=... --disabled

# 新：静默登录（自动启用）
imgw channel login feishu --id feishu:acme:sales --app-id cli_x --app-secret ...

# 旧：列表 + 详情
imgw channel ls
imgw channel show sales

# 新：
imgw channel list
imgw channel list sales

# 旧：停 + 删
imgw channel disable sales
imgw channel rm sales

# 新：
imgw channel logout sales
imgw channel delete sales --yes
```

---

## 8. 已知限制

- `logout` **不能**在平台侧吊销应用（飞书/钉钉/企微需去各自后台；微信/QQ 视平台语义）；
- 未完成的扫码会话是 daemon 内存态，重启即丢（重新 `channel login`）；
- 微信 iLink 二维码/会话 TTL 较短，过期需重新生成；
- `channel login` 创建飞书应用时若飞书平台策略拒绝（`21001 policy`），凭据仍会保存（需平台侧处理后再 `login <ref>` 重连）。
