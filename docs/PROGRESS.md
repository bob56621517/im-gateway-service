# 进度报告（2026-11-10）

> 目标：把 octop-gateway 包成单进程 IM 网关服务，并完成「真实平台扫码 → 网关收 → 后端回显 → 平台回复」的端到端验证。
> 命令行完整参考见 [CLI.md](CLI.md)。

## 0. 一句话结论

- **微信扫码链路已完整跑通**（扫码登录 → 长轮询收 → processor → dev-backend → 自动 ACK 回复 → 聊天记录落库）。
- **飞书**：扫码建应用成功，但飞书以 `21001 platform policy` 拒绝签发 tenant_access_token；已用官方 `lark-cli` 交叉验证为**平台/租户策略**问题，非本仓库代码；凭据会保留，待账号/应用策略解决后重连。
- 企微 / QQ 扫码与静默注册：**待执行**。
- 代码门禁全绿：`463 passed`、`ruff`、`mypy`（strict）。

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

## 2. 真机验证矩阵

| 平台 | 扫码 | 静默凭据 | 状态 |
|---|---|---|---|
| 微信 | ✅ | ✅（`imgw send` 主动推送） | **完整闭环**：扫码登录 → 入站解析 → dev-backend ACK → 聊天记录 in/out → `chat_type=direct` |
| 飞书 | ⚠️ 建应用成功 | ⏸ | 飞书 `21001 platform policy` 拒绝 tenant_access_token；`lark-cli` 交叉验证为租户/账号策略；凭据已保留，待解决 |
| 企微 | ⏳ | ⏳ | 待执行 |
| QQ | ⏳ | ⏳ | 待执行 |
| 钉钉 | — | — | 未开始（不在本轮范围） |

微信验证证据（daemon `.smoke` 实例）：

- `messages(in)` = `run test 微信`；
- `messages(out)` = dev-backend ACK 全文；
- `backends` 表 = **0 行**（证明 `run-dev` 只做内存路由覆盖）；
- 幂等键 = `weixin:wx:<平台 msg_id>`。

## 3. 测试与门禁

```text
uv run pytest -q            463 passed
uv run ruff check src tests  All checks passed
uv run mypy                  Success: no issues found (28 source files)
```

新增测试：`tests/test_dev_backend.py`（SSE 回显/错误帧/token/路由 override）、login 5 平台 adapter、CLI 新命令与输出契约、空 token 头、`chat_type` 默认值等。

## 4. 未完成 / 下一步

1. **企微扫码** → 用户扫 `work.weixin.qq.com/ai/qc` 的 auth_url，拿到 `bot_id`/`secret`；
2. **QQ 扫码** → 手机 QQ 扫 q.qq.com 绑定页；
3. **企微 / QQ 静默注册** → 用各自后台已创建机器人的 `bot_id/secret`、`app_id/secret`；
4. **飞书** → 换企业账号/已发布的「企业自建应用」凭据（或解除租户数据安全策略）后 `channel login feishu:smoke --app-id … --app-secret …`；
5. **capability 真机校准**（`core/capabilities.py` 当前为初值）；
6. **冒烟流程固化**：`scripts/smoke/` + `pytest -m live` + `docs/SMOKE.md`；
7. **开机自启**：`imgw install-service`（NSSM/systemd）。

## 5. 如何复现当前验证

```bash
# 终端 A：开发模式 daemon（内置回显后端，不写 backends 表）
uv run imgw run-dev

# 终端 B：查看状态 / 历史
uv run imgw channel list
uv run imgw history --limit 5

# 扫码登录（微信示例；--qr-png 便于 GUI/手机扫描）
uv run imgw channel login weixin:wx --qr-png .smoke/weixin-qr.png

# 主动推送
uv run imgw send weixin:wx "<subject_id>" "测试推送"
```

> 真实凭据与运行数据放在仓库外/`.smoke/`（已 gitignore）；仓库内不落任何平台 secret。

## 6. 本轮产物

- `docs/CLI.md`：命令行参考 + 命令变更记录（本文链接）；
- `docs/PROGRESS.md`：本文件；
- `src/im_gateway/dev/backend.py`：调试用回显后端套件；
- `src/im_gateway/core/credentials.py`：凭据字段映射 + `ok/missing` 摘要；
- `src/im_gateway/gateway/login.py`：5 平台 adapter + 凭据持久化/告警；
- CLI/控制面/测试/文档更新见 `git log`。
