# 进度报告（2026-11-10）

> 目标：把 octop-gateway 包成单进程 IM 网关服务，并完成「真实平台扫码 → 网关收 → 后端回显 → 平台回复」的端到端验证。
> 命令行完整参考见 [CLI.md](CLI.md)。

## 0. 一句话结论

- **微信 / 企微 / QQ / 钉钉四条链路全部跑通**（扫码登录 → 入站解析 → processor → dev-backend → 自动 ACK 回复 → 主动 `imgw send` 推送 → 聊天记录落库）。
- **飞书**：扫码建应用成功，但飞书以 `21001 platform policy` 拒绝签发 tenant_access_token；已用官方 `lark-cli` 交叉验证为**平台/租户策略**问题，非本仓库代码；凭据会保留，待账号/应用策略解决后重连。
- 静默凭据注册：QQ / 钉钉 / 企微 adapter 均已具备，**待真机验证**。
- 代码门禁全绿：`466 passed`、`ruff`、`mypy`（strict）。

## 1. 已完成

### 1.1 CLI 重构（破坏性，无别名）

| 命令 | 语义 |
|---|---|
| `imgw channel login [TARGET]` | 绑定/重登：扫码建号、静默凭据、已有凭据重连、`--rescan` 强制重扫 |
| `imgw channel logout <REF>` | 停用（保留凭据/数据/历史），之后可 `login` 直接重连 |
| `imgw channel list [REF]` | 默认带表头 TSV；`--no-header` 纯数据；`REF` 看详情（脱敏 config） |
| `imgw channel delete <REF>` | 删行；**必须先 logout**（服务端 409 守卫）；`--purge` 级联；`--yes` |
| `imgw channel test <KIND>` | probe 凭据，不注册 |
| `imgw run` / `imgw run-dev` | 生产入口 / 开发模式（内置回显后端接管所有 tenant） |

输出契约：**stdout=数据，stderr=提示**；secret 在 `config` 视图中一律 `***`（`${ENV}` 原样）。

`--name` 只在**新建通道**时生效：`channel_id` 变为 `<kind>:<name>`，并写入 `name`/`alias`；不传则随机后缀 `<kind>:<6hex>`。扫码过程中钉钉/企微侧的机器人名与网关通道名无关。

### 1.2 五平台扫码 adapter

`src/im_gateway/gateway/login.py`：

| 平台 | 实现 |
|---|---|
| 飞书 | `lark_oapi.register_app`（device flow + `addons` 预置 scope/事件） |
| 企微 | `work.weixin.qq.com/ai/qc`（sCode 扫码，返回 bot_id/secret） |
| 钉钉 | `oapi.dingtalk.com/app/registration`（device flow） |
| QQ | octop `QQBotQRLogin`（q.qq.com 绑定页） |
| 微信 | octop `WeixinQRLogin`（iLink 二维码） |

登录成功会**先持久化凭据再尝试启用**；启用失败（如飞书策略拒绝）仍保留凭据并返回告警。

### 1.3 dev-backend 调试套件 + `run-dev`

- 源码：`src/im_gateway/dev/backend.py`（`create_dev_backend_app()` + `python -m im_gateway.dev.backend`）；
- `imgw run-dev`：进程内起回显后端（默认 `127.0.0.1:9900`），用 `BackendRouter(override=...)` 接管**所有 tenant**；
- 不写 `backends` 表，其余机制（messages/subjects/dedup/audit）照旧；
- 回显一次返回：`channel_id / kind / tenant_id / subject_id / subject_name / chat_type / session_key / turn_id / idempotency_key / message`。

### 1.4 本轮真实 bug 修复

| # | 问题 | 修复 |
|---|---|---|
| 1 | `channel list` 默认无表头 | 默认带表头，新增 `--no-header` |
| 2 | WeChat `qrcode_img_content` 被当图片下载（存成 HTML） | 改为把落地页 URL **编码成二维码** |
| 3 | Windows GBK 控制台打印二维码字符崩溃 | 编码失败自动降级为只输出 URL（提示用 `--qr-png`） |
| 4 | backend token 为空时发送 `Authorization: Bearer `（非法头） | 空 token 不发送该头 |
| 5 | 登录成功但启用失败时凭据被回滚丢失 | 先落库（`enabled=false`）再启用，失败保留并告警 |
| 6 | 微信私聊 `chat_type` 为空 | 仅对 `weixin` 在 `start` 帧补 `direct` |
| 7 | QQ 扫码凭据键名写错：落库 `app_secret`，上游 `QQConfig` 要求 `secret` | adapter 与 CLI `--app-secret` 对 qq 统一映射为 `secret` |
| 8 | Windows GBK 控制台执行 `imgw history` 遇 emoji（回显里的 ✅）直接 `UnicodeEncodeError` | CLI 启动时 stdout/stderr `errors="replace"` 降级 |
| 9 | `GET /v1/channels/{ref}/subjects` 500/400：企微 subject metadata 带活 `WSClient`，FastAPI 无法序列化 | `runtime._json_safe()` 统一降级为 JSON 原生类型（DB 侧原本就有 `_durable_subject_metadata` 过滤） |
| 10 | 钉钉扫码后 CLI 立刻报「二维码已过期」：`/app/registration/poll` 在扫码后返回 `SCANNED`，我们只认 WAITING/SUCCESS/FAIL | 扫描后的 `SCANNED / CREATING / PUBLISHING / APPROVING` 及未知状态一律继续轮询；`APPROVING` 若已带回凭据则保存并告警「待管理员审批」 |
| 11 | 钉钉 poll 凭据字段大小写/位置不确定 | 兼容 `client_id/clientId/app_key/appKey` 等别名，任意一轮带回凭据即接受；状态变化打 WARNING 日志（不记值） |

## 2. 真机验证矩阵

| 平台 | 扫码 | 主动推送 | 入站→ACK | 状态 |
|---|---|---|---|---|
| 微信 | ✅ | ✅ | ✅ | **完整闭环**：扫码登录 → 入站解析 → dev-backend ACK → 聊天记录 in/out → `chat_type=direct` |
| 企微 | ✅ | ✅ | ✅ | **完整闭环**：`wecom:eac0ff`，subject `ShiXuYun`，入站 + ACK + `imgw send` 均成功 |
| QQ | ✅ | ✅ | ✅ | **完整闭环**：`qq:6c2ed2`，subject `E88BAD83…`，修复键名后可重连/启用 |
| 钉钉 | ✅ | ✅ | ✅ | **完整闭环**：`dingtalk:ce5369`，subject `04160852256921482021`，修复状态机后扫码成功 |
| 飞书 | ⚠️ 建应用成功 | ⏸ | ⏸ | `21001 platform policy` 拒绝 tenant_access_token；`lark-cli` 交叉验证为租户/账号策略；凭据已保留，待解决 |

验证证据（daemon `.smoke` 实例，`imgw history`）：

- 每个平台均出现 `in inbound`（用户发给机器人）+ `out message`（dev-backend ACK）；
- 主动推送均返回 `sent to <channel>/<subject>` 且平台侧收到；
- `backends` 表 = **0 行**（证明 `run-dev` 只做内存路由覆盖）。

## 3. 测试与门禁

```text
uv run pytest -q            466 passed
uv run ruff check src tests  All checks passed
uv run mypy                  Success: no issues found (28 source files)
```

新增测试：`tests/test_dev_backend.py`（SSE 回显/错误帧/token/路由 override）、login 5 平台 adapter、CLI 新命令与输出契约、空 token 头、`chat_type` 默认值、QQ `secret` 映射、钉钉状态机、subjects metadata 序列化等。

## 4. 未完成 / 下一步

1. **静默注册真机验证**：企微 `--bot-id/--secret`、钉钉 `--app-key/--app-secret`、QQ `--app-id/--app-secret`（扫码已产出凭据，可 logout 后用同一凭据重新登录验证）；
2. **飞书** → 换企业账号/已发布的「企业自建应用」凭据（或解除租户数据安全策略）后 `channel login feishu:smoke --app-id … --app-secret …`；
3. **capability 真机校准**（`core/capabilities.py` 当前为初值）；
4. **冒烟流程固化**：`scripts/smoke/` + `pytest -m live` + `docs/SMOKE.md`；
5. **开机自启**：`imgw install-service`（NSSM/systemd）。

## 5. 如何复现当前验证

```bash
# 终端 A：开发模式 daemon（内置回显后端，不写 backends 表）
uv run imgw run-dev

# 终端 B：查看状态 / 历史
uv run imgw channel list
uv run imgw history --limit 5

# 扫码登录（任意平台；--qr-png 便于 GUI/手机扫描）
uv run imgw channel login dingtalk --name dingtalk --qr-png .smoke/dingtalk-qr.png

# 主动推送
uv run imgw send <channel_id> "<subject_id>" "测试推送"
```

> 真实凭据与运行数据放在仓库外/`.smoke/`（已 gitignore）；仓库内不落任何平台 secret。

## 6. 本轮产物

- `docs/CLI.md`：命令行参考 + 命令变更记录（本文链接）；
- `docs/PROGRESS.md`：本文件；
- `src/im_gateway/dev/backend.py`：调试用回显后端套件；
- `src/im_gateway/core/credentials.py`：凭据字段映射 + `ok/missing` 摘要；
- `src/im_gateway/gateway/login.py`：5 平台 adapter + 凭据持久化/告警；
- CLI/控制面/测试/文档更新见 `git log`。
