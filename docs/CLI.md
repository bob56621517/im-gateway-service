# CLI：用法文档入口与命令变更记录

> **本文件不再是命令参考。** `imgw` 的权威用法文档就是帮助输出本身：
>
> ```bash
> uv run imgw --help                 # 概览：快速开始、命令分组、环境变量、退出码
> uv run imgw <command> --help       # 单个命令：描述、全部参数、2~5 个可直接复制的示例
> uv run imgw channel login --help   # 例：登录（扫码 / 静默凭据 / 重连 / --rescan / --no-wait）
> uv run imgw channel delete --help  # 例：删除（必须先 logout，服务端 409 守卫；--purge、--yes）
> ```
>
> 帮助输出是随代码一起演进的，因此不会出现「文档与实现漂移」。本文件只保留**语言切换说明**与**破坏性命令变更历史**（旧命令 → 新命令、行为变化、已知限制），不删除历史记录。
> 设计背景见 [HANDOVER.md](../HANDOVER.md) §13.6~§13.8。

---

## 1. 语言（帮助与错误信息）

- **默认英文**；帮助、进度与错误文案支持英文 / 中文两种语言。
- **自动检测**：操作系统区域设置为中文（Windows UI 语言、`LANG` / `LC_ALL` / `LC_MESSAGES` 为 `zh_CN`、`zh-CN`、`GBK` / codepage `936` 等）时自动使用中文。
- **显式覆盖**：`IMGW_LANG=en` 强制英文；`IMGW_LANG=zh` 强制中文（也接受 `zh_CN` / `zh-CN` / `zh_TW` 等变体，大小写不敏感）。注意变量名是 **`IMGW_LANG`**，`IMG_LANG` 之类的拼写不会被识别。
- **不变的部分**：机器可读输出在两种语言下都保持英文 —— JSON 键、TSV 表头、channel/subject id、时间戳、`sent to ...` / `deleted: ...` 行、secret 掩码（`***`、`===***===`）与退出码。
- **daemon 侧文案**：守护进程返回的错误详情目前仍为中文（本次只翻译 CLI 侧文案，见 HANDOVER §12/§13 的分层约定）。

示例：

```bash
uv run imgw --help                                  # 跟随系统语言
IMGW_LANG=zh uv run imgw --help                     # 强制中文
IMGW_LANG=en uv run imgw --help                     # 强制英文
IMGW_LANG=zh_CN.UTF-8 uv run imgw channel login --help
```

---

## 2. 输出与退出码约定（仅保留契约本身）

- **stdout = 数据，stderr = 提示**：`| grep` / `| awk` 只会拿到数据行；进度、告警、错误一律走 stderr。
- 全局参数：`--url`（或 `IMGW_URL`）、`--token`（或 `IMGW_TOKEN` / `IMGW_TOKEN_FILE` / token 文件）、`--json`。
- 退出码：成功 `0`；失败 / 超时 / 未就绪 `1`；参数用法错误 `2`（由 CLI 框架报告）。
- 写操作都会落审计；secret 不落日志、不进 `/v1/status`，`channel list` 详情视图里一律 `***`。
- 除 `run` / `run-dev` 外，所有命令只调 daemon 控制面（`--url` + `--token`），不直接读写 SQLite。

---

## 3. 命令变更记录（破坏性，已无旧别名）

### 3.1 命令映射（历史，勿删）

| 旧命令 | 新命令 | 说明 |
|---|---|---|
| `channel add <kind> --id --config` | `channel login <kind> --id --app-id …` | 静默注册统一走 login；`--config` 仍可用 |
| `channel ls` | `channel list` | 默认带表头；默认包含 disabled |
| `channel show <ref>` | `channel list <ref>` | 详情合并进 list（脱敏 config） |
| `channel rm <ref>` | `channel delete <ref>` | **必须先 logout** |
| `channel enable <ref>` | `channel login <ref>` | 凭据在就直接重连启用 |
| `channel disable <ref>` | `channel logout <ref>` | 语义不变（保留凭据/数据） |
| `run --dev-backend` | `run-dev` | 独立子命令 |

### 3.2 行为变化（历史）

1. `channel list` 默认**带表头**；需要裸数据用 `--no-header`；
2. `channel list` 默认**包含 disabled 通道**（`state` 列显示 `disabled`）；
3. `channel login` / `logout` / `list <ref>` 的 config 视图**强制脱敏**（secret → `***`，`${ENV}` 原样）；
4. 扫码登录成功会**自动启用**通道（不再是“登录完还是 disabled”）；
5. `delete` 前必须 `logout`（服务端 409 守卫）；
6. 支持 5 平台扫码：飞书 / 企微 / 钉钉 / QQ / 微信；
7. 空 backend token 时**不再发送**非法 `Authorization: Bearer ` 头；
8. 终端二维码编码失败自动降级 URL（Windows GBK）；
9. 修复微信二维码生成（把落地页 URL 编码成 QR，而不是下载网页）。

### 3.3 已移除（不提供别名）

`channel add`、`channel ls`、`channel show`、`channel rm`、`channel enable`、`channel disable` 已删除，**不提供别名**。

### 3.4 迁移示例

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

## 4. 已知限制（保留）

- `logout` **不能**在平台侧吊销应用（飞书/钉钉/企微需去各自后台；微信/QQ 视平台语义）；
- 未完成的扫码会话是 daemon 内存态，重启即丢（重新 `channel login`）；
- 微信 iLink 二维码/会话 TTL 较短，过期需重新生成；
- `channel login` 创建飞书应用时若飞书平台策略拒绝（`21001 policy`），凭据仍会保存（需平台侧处理后再 `login <ref>` 重连）；
- `--help` 的框架部分（`Usage:`、`Options:`、`Commands:`、内置 `--help` 说明）由 Click/Typer 提供，保持英文；命令描述、参数说明与示例均已本地化。
