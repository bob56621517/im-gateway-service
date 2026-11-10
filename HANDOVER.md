# im-gateway-service 交接文档

| 项 | 值 |
|---|---|
| 日期 | 2026-11-09 |
| 状态 | 方案讨论完成，未开始编码 |
| 范围 | 选型、架构、仓库形态、许可证 |
| 本文档作用 | 记录**问题、过程、结论、被否决项、未决项**，避免重复讨论 |

---

## 0. 一句话结论

> 以 **octop-gateway（MIT，Python 库）** 作为通道适配层，自己写一层 **单进程 Python 服务**（CLI + HTTP 控制面）；
> 入站投递用 **webhook 流式派发（模型 A）+ 单一 `TurnFrame` 信封**（§12）；**配置与运行数据都落 SQLite 单一权威**（§13）；
> 代码结构预留 **四个 seam**，使「多节点」成为同一份代码的第二种部署模式，而不是第二套程序；
> 本仓库代码采用 **AGPL-3.0**，依赖侧 MIT 不构成任何传染。逐项决策见 **§14 定稿清单**。

---

## 1. 目标与约束（需求原话提炼）

1. 需要一个 **IM 网关中间件**：统一接入各大 IM 服务商，供自己的 agent/后端使用。
2. **优先接入大平台**（飞书、企微、QQ、微信、钉钉…），小平台是否接入无所谓——小平台应当主动兼容协议来扩展生态，因此**插件化/热插拔没有价值**。
3. **稳定性压倒一切**，且要求项目能持续迭代、不会死。
4. 模块化/组件化重要，但**最终判据是「网关是否是独立 repo、独立演进」**：
   - 一个仓库里同时承载「通道 + agent」的项目，**一律不采纳**（OpenClaw 是反例）。
5. 形态上：**没有 GUI**，只有 CLI；CLI 与 gateway 通过 HTTP 通信；`run` 前台跑、`start/stop/restart/status` 走标准 Linux 服务玩法。
6. 语言：**统一 Python**（Java 最熟，但两语言方案的复杂度被否）。
7. 商用可行；是否强制开源无所谓（AGPL 可接受），因为**跨进程隔离**是既定架构。
8. 长期保留「自建 IM」的可能性，但**不是现在**。

---

## 2. 讨论过程（决策链）

| # | 问题 | 结论 | 备注 |
|---|---|---|---|
| 1 | 找开源 IM gateway，默认 octop-gateway，其余对比 | 锁定 octop-gateway | 见 §3 数据 |
| 2 | octop 星少，但它是 Octop 的组件、腾讯内部项目开源 | 接受「家族归属」，但仍需区分「代码血统成熟」与「公开仓库运营成熟」 | 见 §3.4 |
| 3 | 应用平台和库无所谓，要功能 | 短暂放开形态，LangBot/AstrBot 一度升为并列第一 | 见 §4 |
| 4 | 但形态放开又违背「独立 repo」原则 | 收紧为硬规则：**通道与 agent 同仓者一律淘汰** | 最终规则 |
| 5 | octop-gateway 是库还是服务？怎么接后端、怎么收回调 | 确认是**纯库**；回调模型是 in-process 异步生成器 | 见 §3.2/§3.3 |
| 6 | 不同通道是否有统一接口 | **接口统一、实现不统一**（统一在中间，分叉在两头） | 见 §3.5 |
| 7 | 能否包成无状态纯转发 OpenAPI 服务 | **不能**；网关有状态，无状态的是外面那层 | 见 §3.6 |
| 8 | 企微是否必须公网 IP | **不必须**：企微走 WS 长连接，零公网 IP 成立 | 见 §3.7，纠正了早前判断 |
| 9 | 自研服务怎么开发（fork / Python 扩展 / Java+GraalPy） | **import + 扩展**；GraalPy 否决；fork 只在必须改内置通道时 | 见 §4 |
| 10 | Java 控制面 + Python 数据面的 repo 怎么分 | 曾建议单仓三层；后因「太复杂」被否 | 见 §4 |
| 11 | 改成统一 Python、CLI + daemon（类似 OpenClaw） | **采纳**：一个包、两个面、loopback HTTP | 见 §5.7 |
| 12 | OpenIM 是什么、能否 MQTT 接入 | **品类不同**（IM 服务端，不是网关）；不支持 MQTT | 见 §4 |
| 13 | channel_id / subject_id 是否支持同平台多实例 | **支持**，受凭证与资源约束 | 见 §5.3 |
| 14 | 先 SQLite + 单进程，后续多节点 | **采纳**，并拆分为「分片 / 选主 / 恢复」三件事 | 见 §5.5 |
| 15 | 许可证：GPL / LGPL / AGPL | **AGPL-3.0**；且因是 import 而非 fork，只能约束自己的代码 | 见 §6 |
| 16 | 入站消息如何交给进程外后端 | **模型 A：webhook 流式派发**（后端实现唯一 `POST /v1/turns`） | §12 |
| 17 | request / response 是否两套结构 | **否**，单一 `TurnFrame` 信封，request = `kind=start` 首帧 | §12 |
| 18 | 一套结构能否涵盖飞书卡片等平台特性 | **语义能、表现不能**；平台特性走 `native` 帧 + `start.capabilities` | §12.7 |
| 19 | 配置放 SQLite / TOML / YAML / JSON | **SQLite 单一权威**；JSON 仅作导入导出格式 | §13、§14.3 |
| 20 | subjects / inbound_dedup 能否放配置 | **不能**：按消息频率写的运行数据，与配置文件语义冲突 | §13.9 |
| 21 | 聊天记录是否落库 | **落**，`messages` + 可清理保留策略；但只作日志，不是会话状态 | §13.9 |

### 2.1 过程中被推翻或修正的判断（重要，防止复述错误结论）

| 早前判断 | 修正后 | 依据 |
|---|---|---|
| 「企微是 Callback + API，需要公网端口」 | **错**。企微走 `wecom-aibot-sdk` 的 WS 长连接，无需公网 | `channels/wecom.py` 源码 |
| 「12 个依赖会被全量导入」 | **错**。`channels/__init__.py` 明确 lazy import | 源码注释 |
| 「LangBot 只能当第二供应商」 | 形态放开后一度升为第一；**「独立 repo」规则恢复后再次淘汰** | 规则迭代 |
| 「多节点 = 高可用」 | **错**。多节点 = 分片 + 选主 + 恢复，三者可独立开关 | §5.5 |
| 「AGPL 可以覆盖 octop」 | **错**。import 模式下只能给自己代码选证 | §6 |

---

## 3. octop-gateway 事实核查（截至 2026-11-09）

### 3.1 仓库数据

| 项 | 值 |
|---|---|
| 仓库 | `TencentCloud/octop-gateway`（TencentCloud 官方组织） |
| 创建 | 2026-09-24（讨论时约 46 天） |
| Star / Fork | 86 / 42 |
| Open issues / PR | 11 / 24（Search API 拆分） |
| Release | v1.0.0(09-24) → 1.0.1(10-09) → 1.0.2(10-14) → **1.0.3(10-16)** |
| 最后提交 | 2026-11-05 |
| 贡献者 | 8 人（含腾讯账号 `jubaoliang-tencent`） |
| 许可证 | MIT |
| 形态 | Python 包（PyPI），637 KB 源码 |
| 元数据 | PyPI 分类器 **`Development Status :: 3 - Alpha`** |
| Python | **>= 3.12** |

### 3.2 形态：纯库，不是服务

- 无 CLI、无 daemon、无 `serve` 子命令；入口是 `import`。
- 典型用法：

```python
manager = ChannelManager(processor=processor, workers_per_channel=4,
                         media_backend=FileSystemMediaBackend("/tmp/media"))
await manager.start()
await manager.add_feishu_channel(FeishuConfig(app_id=..., app_secret=...))
await asyncio.Event().wait()
```

- **进程边界由使用者决定**，库不提供。

### 3.3 集成模型（「回调」的准确含义）

- 平台 → 网关：各通道自己的传输，库内部消化。
- 网关 → 你的后端：**不是 HTTP 回调**，是 `Callable[[InboundMessage], AsyncIterator[MessageEvent]]`，由 manager 拉取。
- 你的后端 → 网关：`push_text` / `push_content` / `push_to_all`。
- 事件类型 10 种：`MESSAGE / DELTA / THINKING / THINKING_DELTA / FLUSH / TYPING / TOOL_START / TOOL_END / ERROR / COMPLETED`，均有工厂方法（`MessageEvent.delta()` 等）。
- 参考实现：`examples/backends/octop_harness.py`（27 KB，完整 agent 接法）。

### 3.4 血统与「不会死」的真实依据

- 家族数据：`TencentCloud/Octop` 11,784★ / 1,619 fork / 762 open issues，创建于 2026-07-08。
- **最强证据：Octop 的 `pyproject.toml` 依赖 `octop-gateway>=1.0.3`（PyPI）**，即公开件 = 生产件，属 dogfooding。这比「大厂承诺」更硬——产品坏则网关坏。
- 但仍需区分：**代码血统成熟 ≠ 公开仓库运营成熟**。仓库运营仅 46 天（发版节奏、社区 PR 流程、快照与内部的漂移都在这段新流程里）。
- 现实风险：团队带宽与 Octop 的 762 个 issue 共享，网关 issue 首响会受产品支持负载影响。

### 3.5 统一接口 vs 统一实现

| 层 | 内容 | 统一？ |
|---|---|---|
| L0 传输 | WS / Stream / Callback / MQTT / long-poll | ❌ |
| L1 解析 | `parse_inbound()` → `InboundMessage` | ✅ |
| L2 编排 | 队列、worker、限流、超时、媒体、push | ✅ |
| L3 渲染 | `_send_text` / `_send_content` / `_send_media` | ❌ |

统一的具体体现：

- `ContentPart` 判别联合（text/image/video/audio/file）。
- 媒体三级来源 `data` → `local_path` → `url`，必须走 `BaseChannel.load_media_bytes()`，禁止自行 decode。
- `ChannelSubject` 作为唯一出站句柄（私聊=用户，群=会话 ID，作者放 `metadata.sender_id`）。
- `GroupContext.capability_degraded` —— 官方承认群上下文存在平台能力差异。

不统一的现实（排期必须考虑）：

- `Config` 各自不同（凭据模型不同）。
- 富消息（卡片/按钮/引用/话题）能力因平台而异，`MessageEvent` 只保证语义不保证表现。
- `ChannelConstraints` 是 per-channel 传参（限流/超时/是否展示思考）。
- 媒体上传路径分叉（例：issue #55 微信 CDN 上传失败被当成功）。

### 3.6 状态性（决定「能否无状态转发」）

**有状态**，逐项：

| 状态 | 位置 |
|---|---|
| 通道长连接 | 进程内存 |
| subject 注册表 | 进程内存（首条消息时建立） |
| 流式累积（DELTA→COMPLETED） | 进程内存 |
| 群上下文缓冲（TTL、回复后清空） | 进程内存 |
| 限流/超时计时 | 进程内存 |
| 去重缓存 | 进程内存 |
| 媒体 | **可外置**（`MediaBackend` 抽象，默认 FileSystem） |
| 持久会话位 | 宿主 DB（`push_routing.py` 注释：cron 从 session row 读） |

结论：**网关本身不可能无状态**；可以无状态的是外面加的那层 API。原因：入站消息只存在于 WS 连接里，没有入口可插；回复还绑着 `_frame` / `_ws_client` 活对象（`push_routing.py` 的 `EPHEMERAL_PUSH_META` 会在主动推送时剥掉它们）。

### 3.7 零公网 IP：成立

| 通道 | 入站方式 | 需公网端口 |
|---|---|---|
| 飞书 | WS + REST | ❌ |
| 钉钉 | Stream | ❌ |
| QQ | WS | ❌ |
| **企微** | **WS（aibot SDK，源码注释明确 "no public URL needed"）** | ❌ |
| Telegram | long-polling | ❌ |
| Discord | Gateway WS | ❌ |
| MQTT | 出站连接 | ❌ |
| 微信 iLink / 元宝 / 小艺 | 未读代码，README 标 `—` | **待确认** |

企微回复三条路径全部出站：`reply_stream`（WS）> `response_url`（HTTP POST）> `send_message`（WS）。

### 3.8 扩展点

- 子类化 `BaseChannel` + `ChannelManager.add_channel(instance)`（**可覆盖内置行为，90% 场景无需 fork**）。
- `register_channel_kind(kind, cls, plugin_id=...)` 运行时注册；**不允许覆盖内置 kind**；插件所有权隔离、可反注册。
- `SUPPORTED_CHANNEL_KINDS` / `registered_channel_kinds()`。

### 3.9 文档漂移（以代码为准）

1. README 平台表写 `WeCom | Callback + API`，实际是 WS 长连接。
2. `models.py` 中 `ChannelSubject` docstring 提到 `to_handle` 字段，dataclass 实际没有该字段。

---

## 4. 被否决的方案

| 方案 | 否决理由 |
|---|---|
| **OpenIM / WuKongIM / Tinode** | **品类错误**：自托管 IM 服务端（拥有用户/消息），不是接别人的桥。用户还得装你的客户端。 |
| **OpenClaw** | 单仓同时含通道 + agent，且 13.5 GB / 3,088 open PR，问题队列不适合做依赖。 |
| **LangBot / AstrBot** | 同为「应用平台」（通道 + agent + pipeline + WebUI），违反「独立 repo」硬规则。AstrBot 另有 743 issue / 437 PR 噪音。 |
| **Matterbridge** | 唯一同形态纯中间件，但 2023-01 后无 release、2024-12 后无提交，已停更。 |
| **mautrix 系列** | 每通道一 repo、AGPL、维护良好，但 hub 是 Matrix，不是你的 agent；且无国内平台。仅作通道级补位。 |
| **Java + GraalPy 单进程** | 依赖含 pydantic-core(Rust)/cryptography(Rust)/aiohttp(C) 等，兼容性风险高；且「想用 Java 写服务」用进程边界即可解决。 |
| **fork + 改名 `octop-gateway-service`** | 只是加服务外壳，全为新增文件；fork 会失去「pin 版本即得修复」的能力，而该项目正处于高频修复期。**仅在必须改内置通道行为时才 fork。** |
| **Java 控制面 + Python 数据面（monorepo 三层）** | 被判「太复杂」，改为统一 Python。 |
| **MQTT 桥接 OpenIM** | OpenIM 不支持 MQTT；硬桥需要 4 跳、2 套 schema、2 个 sidecar。正解是 webhook 进 + REST 出，写一个 `OpenIMChannel`。 |
| **无状态纯转发 OpenAPI 服务** | 见 §3.6，网关有状态、入站无入口，做不到。 |

---

## 5. 架构决策

### 5.1 两种部署形态，一份代码

| 形态 | 形态内容 | 适用 |
|---|---|---|
| **单进程（all-in-one）** | 一个进程 = CLI + HTTP 控制面 + ChannelManager + SQLite | v1 |
| **多节点** | 分片 + 选主 + 接管恢复 | v2（关键通道） |

**铁律：多节点必须是同一份代码的第二种部署模式。** 差异收敛到四个可注入依赖，业务逻辑不得出现 `if cluster:`。

### 5.2 四个 seam

```python
class ChannelRepo(Protocol):   # SQLite ↔ Postgres
class LeaseStore(Protocol):    # 单进程: 本地锁 / 多节点: Redis
    async def acquire(channel_id, ttl) -> Lease | None
    async def renew(lease) -> bool
    async def release(lease) -> None
class Ownership(Protocol):     # 单进程: 全要 / 多节点: 哈希分片
    def owns(channel_id) -> bool
class CursorStore(Protocol):   # 单进程: 空实现 / 多节点: 持久化 offset
```

单进程实现全是空实现，**但代码路径必须一致**，否则多节点路径永远没测试覆盖。

### 5.3 channel_id / subject_id 与多实例

三层：`kind` → `channel` → `subject`（微信多一层 `accounts`）。

- 同 kind 可注册任意多实例，按 `channel_id` 隔离。
- 真实上限：**凭证数**（同凭证开两实例会互踢、重复投递）、平台限流（按凭证）、进程资源（`workers_per_channel` 是乘法）。
- **`channel_id` 必须显式指定**（不传则自动 UUID，重启即漂移）；建议 `feishu:tenant-a:sales` 式命名，作为全局路由主键。
- 数据键必须是 `(channel_id, subject_id)` 复合；同一个人在两个应用里是两个 subject。

### 5.4 数据模型（SQLite v1）

```sql
channels(channel_id TEXT PK, kind TEXT, tenant_id TEXT,
         config_json TEXT, constraints_json TEXT, enabled INT, created_at)

subjects(channel_id TEXT, subject_id TEXT, display_name TEXT, chat_type TEXT,
         first_seen REAL, last_seen REAL,
         PRIMARY KEY(channel_id, subject_id))

idempotency(key TEXT PK, channel_id TEXT, created_at REAL)

audit(id INTEGER PK, ts REAL, actor TEXT, action TEXT, payload_json)
```

不落库：消息正文、媒体二进制、大日志。

SQLite 运维三条：开 WAL；单写者（避免 `database is locked`）；禁止放网络盘；备份用 `VACUUM INTO`。

> **2026-11-09 更新：本节 schema 已被 §13.2 取代**（新增 `backends`、`subjects.metadata_json`、`alias`、迁移版本等）。

### 5.5 演进路线

| 版本 | 形态 | 内容 | 触发条件 |
|---|---|---|---|
| **v1** | 单进程 | SQLite + systemd + 四个 seam 的空实现 | — |
| **v1.5** | 多节点-lite | **仅分片 + 监督重启**，不做选主 | 通道数 > 30，或出现租户拖垮全组 |
| **v2** | 多节点-full | 仅对关键通道开租约 + fencing + 游标恢复 | 出现「断 30s 会出事」的通道 |

**关键认知**：

- 「多节点」不是「多副本」——平台连接不可共享，同凭证双连会互踢。
- **HA 只能做到「主备切换 + 重连」，SLO 应写成「中断 < 30s」，不是「零中断」。**
- 选主必须配 **fencing token**（`owner` + 单调 `epoch` + `expires_at`，取租约存储的时钟）；否则双主互踢会比不选主更糟。
- 多节点**不消灭单点**：协调存储（Redis/etcd）成为新单点，它自身必须 HA。

### 5.6 CLI / daemon 形态

一个包、两个面：

```
cli（typer）──HTTP/JSON(127.0.0.1 + token)──▶ gateway（uvicorn + FastAPI + ChannelManager 同 loop）
```

- CLI 只依赖 `httpx` + `typer`，**不 import core**（避免拉起 10 个平台 SDK）。
- 契约用 pydantic 模型，CLI 与 daemon 共用，防漂移。
- `imgw run` 是唯一真正启动进程的命令；其余全是 HTTP 客户端。
- **不要自己 daemonize**：`install-service` 生成 systemd / launchd / NSSM 配置，`start|stop|restart` 转发给系统。
- `start` 默认 `--wait`：轮询 `/readyz` 直到通道 connected 或超时，避免 `start && send` 竞态。
- 控制 API 最小集：`/healthz`、`/readyz`、`/v1/status`、`/v1/channels`(CRUD)、`/v1/channels/{id}/login`、`/v1/messages`（带 `Idempotency-Key`）、`/v1/events`(SSE)、`/v1/reload`。
- 二维码：Python 用 **`segno`**（`segno.make(url).terminal(compact=True)`），备选 `qrcode.print_ascii()`；需处理深色终端反色与终端宽度。
- 坑：控制 API 与流式回复同 loop，控制面只许 await，重活 `to_thread`；`start` 成功 ≠ 可用。

### 5.7 目录（单包）

```
src/im_gateway/
├── cli/        main.py / qr.py / client.py
├── gateway/    app.py / routes.py / manager.py / events.py
└── core/       config.py / contracts.py / paths.py / service.py / repos/
tests/
```

entry point 只需一个：`imgw = "im_gateway.cli.main:app"`。

---

## 6. 许可证结论

**前提修正：本项目是 `import octop_gateway`（MIT 依赖），不是 fork。**

1. **MIT 不传染**，`import` 不带来任何开源义务；可以自由选择自己代码的许可证。
2. 因此「把 octop 换成 GPL/AGPL」不成立——那需要版权方同意；能决定的只有**自己代码**的许可证。
3. 若要「强制开源」，因为交付形态是**网络服务**，只有 **AGPL-3.0** 有效（GPL 只在分发二进制时触发；LGPL 反向不传染）。
4. 本仓库 `LICENSE` 已是 **AGPL-3.0**，与结论一致。
5. **唯一的 MIT 义务**：分发（含 Docker 镜像）时附上 octop-gateway 的 LICENSE 与版权声明 → 建 `THIRD_PARTY_NOTICES.md`。
6. 插件/适配器 SDK 建议 **Apache-2.0**，避免 AGPL 吓退插件作者；是否双许可取决于是否引入 CLA（要双许可 → CLA；纯社区 → DCO）。
7. 注意：AGPL 挡不住「云厂商公开源码后照卖」；能挡的是 SSPL/BUSL 一类，但那已不是开源。

---

## 7. 未决问题 / 待确认

| # | 事项 | 影响 | 建议动作 |
|---|---|---|---|
| 1 | 微信 iLink / 元宝 / 小艺 的入站传输是否出站 | 关系到「零公网」是否对所有通道成立 | 读 `channels/weixin|yuanbao|xiaoyi` |
| 2 | MQTT 通道的 payload/topic 契约 | 决定能否复用为通用接入 | 读 `channels/mqtt.py` |
| 3 | `ChannelConstraints` 各通道默认值与行为差异 | 上线前需逐通道校准 | 读 `constraints.py` + 实测 |
| 4 | 是否引入 OpenIM 作为第 11 通道 | 仅当需要「自有界面聊天」时 | 暂缓，先用 30 行 `webchat` 验证 |
| 5 | CLA 还是 DCO | 决定能否商业双许可 | **尽早定，事后不可补** |
| 6 | 多节点协调层选型（Redis vs etcd） | v2 才需要 | 暂缓 |
| 7 | 是否需要商业双许可 | 影响 CLA 与许可证表述 | 暂缓 |
| 8 | 二维码终端兼容（Windows Terminal / 深色主题） | CLI 体验 | 用 segno 实测 |

> **2026-11-09 第二轮更新：#1 / #2 / #3 已由源码核对关闭，见 §11.1；#4~#8 状态不变。**

---

## 8. 风险登记

| 风险 | 说明 | 缓解 |
|---|---|---|
| 上游高频变更 | 3 周 4 个 release、24 open PR、Alpha 分类器 | pin `==1.0.3`；`make bump-gateway` 做升级闸门 |
| 内置通道 bug | 需改 54 KB 的 `feishu.py` 级别代码 | 优先子类化 + `add_channel()`；必要时最小 patch 并记录 `UPSTREAM.md` |
| 平台 API 变更 | 例：微信 CDN 上传失败被当成功（issue #55） | 逐通道 parity/冒烟测试；监控发送失败率 |
| 单源依赖 | 满足「独立 repo + 多通道 + 活跃」的只有 octop-gateway | 代码级第二源：MIT + 637 KB，可 fork 自持 |
| 团队带宽共享 | Octop 762 issue 与网关共用人力 | 自建 issue 首响预期；关键 bug 自行修 |
| 文档漂移 | README 与代码不一致（见 §3.9） | 一切以源码为准；fork 后顺手修正 |

---

## 9. 下一步（建议顺序）

1. 写 `core/contracts.py`：`InboundEvent` / `SendRequest` / `Capability` 三个 pydantic 模型。
2. 写 30 行 `webchat` 通道（WS + 内存 session），验证「自建通道 = 普通通道」的抽象成立。
3. 搭 CLI + FastAPI 骨架，跑通 `imgw run` / `status` / `channel add` / `send`。
4. 落 SQLite repo（四个 seam 的空实现）。
5. 单通道（飞书）端到端：入站 → processor → 回复；再补企微 WS。
6. 建立双模式 CI（1 节点 SQLite / 3 节点 Redis+PG），即使 v2 未实现也要先占位。

---

## 10. 参考资料

- octop-gateway：<https://github.com/TencentCloud/octop-gateway>
- octop-gateway README（PyPI 元数据）：<https://pypi.org/pypi/octop-gateway/json>
- Octop（宿主，依赖 `octop-gateway>=1.0.3`）：<https://github.com/TencentCloud/Octop/blob/main/pyproject.toml>
- 通道注册表：<https://github.com/TencentCloud/octop-gateway/blob/main/src/octop_gateway/channels/__init__.py>
- 统一消息模型：<https://github.com/TencentCloud/octop-gateway/blob/main/src/octop_gateway/models.py>
- 企微 WS 实现：<https://github.com/TencentCloud/octop-gateway/blob/main/src/octop_gateway/channels/wecom.py>
- OpenIM（品类对照）：<https://github.com/openimsdk/open-im-server> ／ webhook 文档：<https://docs.openim.io/zh/platform-api/webhooks/overview>
- Matterbridge（已停更，对照）：<https://github.com/42wim/matterbridge>
- mautrix 桥（通道级补位参考）：<https://github.com/mautrix/whatsapp>、<https://github.com/mautrix/telegram>

---

## 术语

| 术语 | 含义 |
|---|---|
| channel | 一个平台实例（一套凭证 + 一条连接 + 一个 subject 命名空间） |
| subject | 会话对象（私聊用户 / 群会话） |
| seam | 可替换的依赖边界（repo / lease / ownership / cursor） |
| fencing | 用单调 epoch 拒绝过期持有者的写入，防止双主 |
| dogfooding | 宿主产品直接依赖开源件，公开件即生产件 |

---

## 11. 第二轮：对 octop-gateway **源码**的核对（v1.0.3 @ `224ed07`）

> 2026-11-09 第二轮补充。本地 checkout 即 v1.0.3 tag，与 pin 版本一致。
> **一切以源码为准**；README 与 AGENTS.md 已知漂移（AGENTS.md 连 typing 默认值表都是错的），不再引用。

### 11.1 §7 的三个未决项 —— 关闭

**(1) 微信 iLink / 元宝 / 小艺的入站传输**

| 通道 | 入站 | 公网回调 | 凭据 / 登录 | 出站 | 性质 |
|---|---|---|---|---|---|
| weixin (iLink) | HTTP 长轮询 `POST ilink/bot/getupdates`（35s 挂起） | **不需要**（源码注释 "No public callback URL required"） | bot_token；扫码 `get_bot_qrcode` → 轮询 → `bot_token/account_id/base_url` | REST `sendmessage` | **逆向**：无 SDK，硬编码域名/版本/客户端指纹头 |
| yuanbao | 常驻二进制 WS `wss://bot-wss.yuanbao.tencent.com/wss/connection` | 不需要 | app_key + app_secret → `sign-token` → WS `auth_bind` | 同一 WS | **逆向**：自研 ConnMsg 编解码，硬编码实例号/版本号 |
| xiaoyi | 双常驻 WS（primary + backup） | 不需要 | ak/sk/agent_id，HMAC-SHA256 签名头 | 同一 WS | **逆向**：backup 是硬编码公网 IP 且 `ssl=False`（关掉证书校验） |

结论：**「零公网 IP」对全部 10 个通道成立**，§3.7 待确认项可关闭。

但必须新增一条认知：**weixin / yuanbao / xiaoyi 是逆向/私有协议，不是「官方机器人平台」**（全仓无免责声明）。
运营后果：weixin `errcode=-14` 只能重新扫码并暂停 300s；yuanbao 单凭证只允许一条有效 WS（故 `probe_channel` 特判只走 sign-token，`manager.py:604-610`）；xiaoyi 重连上限 50 次、backup 走明文信任。
→ 通道必须分层（见 §11.4 D2）。

**(2) MQTT 通道契约**

- topic：订阅 `subscribe_topic` 默认 `devices/+/in`；发布 `publish_topic` 默认 `devices/{client_id}/out`（`{client_id}` 取 `subject.metadata`）；QoS 默认 2；MQTTv311，client_id 随机，`clean_session=True`。
- payload：入站 UTF-8 文本，JSON 只认 `text` 字段（可带 `redirect_client_id`）；出站**永远纯文本**，无信封、无 message_id、无 ack、断连静默丢弃；媒体降级为 `[label: url]`。
- subject：固定 `chat_type="direct"` + 设备 client_id；`channel_session_id` 是随机连接 id。

**结论：不能作为对外通用接入协议**（缺信封/关联 id/应用层 ack/稳定 session）。需要「自有客户端」时按 §7-4 写 30 行 webchat 通道。

**(3) 各通道 `ChannelConstraints` 默认值**

| 通道 | reply_timeout | send_rate_limit | typing_keepalive | timeout_strategy |
|---|---|---|---|---|
| wecom | **10.0** | **(2, 5)** | 0 | **placeholder** |
| weixin | 0（`__init__` 强制归零） | (5, 60) | 0 | none |
| yuanbao | 0 | (2, 5) | 2.0 | none |
| qq | 0 | (20, 60) | 5.0 | none |
| telegram | 0 | (20, 60) | 4.0 | none |
| mqtt | 0 | (30, 60) | 0 | none |
| xiaoyi | 0 | (20, 60) | 0 | none |
| discord | 0 | None | 8.0 | none |
| dingtalk / feishu | 0 | None | 0 | none |

三个必记的坑：

1. **`show_thinking` / `show_tool_hints` 会被 config 覆盖**（`channel.py:245-247`，且 `ChannelConfig` 默认 `False/True`）。所以 weixin 在 `_default_constraints` 里声明的 `show_tool_hints=False` **实际不生效**；要改只能在 config 层改。
2. **`reply_timeout` 在 0.8 倍时刻触发**占位消息（`constraints.py:225-233`），配置值 ≠ 实际触发时刻。
3. **`debounce_seconds` 是死参数**：全库只有赋值，没有任何计时逻辑；所谓 batching 是同会话的「即时 drain」而非时间窗去抖。本文件若出现「去抖」表述应改。

### 11.2 宿主契约：本文件此前没写、但外壳必须遵守的事实

| # | 事实（源码） | 对外壳的影响 |
|---|---|---|
| 1 | `MessageProcessor = Callable[[InboundMessage], AsyncIterator[MessageEvent]]`，调用点是 `events = processor(msg)` 后直接 `async for`（`channel.py:836-837`） | processor 必须是**异步生成器**，不能写成「`async def` 返回 async iterator」的 coroutine；否则 `async for` 直接 TypeError |
| 2 | processor 抛异常被上游 `except Exception` 吞掉，向用户发**固定英文** "An error occurred while processing your message."，flush 已累积 delta（`channel.py:906-922`） | 错误文案/重试/降级都必须在 processor 内自己做（yield `error_event`），拿不到上游扩展点 |
| 3 | `DELTA` 只累积，**只有 `COMPLETED`/`FLUSH` 才真正发出**；`ERROR` 是终止事件，发完即 `break` 并关闭流 | 后端流式协议必须保证终止帧；「流到一半断连」会丢弃已累积文本 |
| 4 | `manager.push_text/push_content` **无返回值**；未连接时行为三态：抛异常（wecom/yuanbao）/ 静默 return（weixin/telegram/xiaoyi/mqtt）/ 只 warn（`push_to_all` 逐 subject 吞异常） | 必须包一层 outbox：统一结果、重试、去重；`/v1/messages` 不能直接透传「成功」 |
| 5 | **没有统一连接状态 API**：仅 discord 有公开 `is_connected`/`runtime_error`；其余只有私有 `_connected`，feishu/qq/weixin/telegram 连私有标志都没有 | `/readyz` **不能**承诺「通道已连接」；只能定义成「进程+DB 就绪 + 通道已注册」 |
| 6 | `manager.start()` 对单通道启动失败**只记日志不抛**（`manager.py:163-169`） | 启动后半启动不可见 → 必须自己维护 per-channel 状态并暴露 |
| 7 | `manager.stop()` 无 drain、无 grace：先 cancel worker/gather，再停通道，最后清队列（`manager.py:193-217`） | 优雅停机要自己加：先停收新流量 → 等 in-flight ≤ N 秒 → 再 `manager.stop()`；feishu stop 可能等线程 join 10s，需抬高超时 |
| 8 | `remove_channel` cancel 该通道 worker 但**不 await**（`manager.py:680-697`） | disable/变更配置前要自己等待 worker 退出 |
| 9 | 无 `reconnect` / `restart` / `enable` / `disable` / `update_config` API | 换配置唯一路径 = `remove_channel` + `add_channel`；禁用靠外壳层跳过注册 |
| 10 | 每通道队列满（默认 1000）**静默丢弃**，只记 error；无背压 API（`manager.py:269,294`） | 入站高峰会丢消息 → 需要监控队列深度 + 显式丢弃计数（或自己的持久化入站层） |
| 11 | subject 注册表纯内存（`_known_subjects`），且 `set_on_new_subject` 只在**首次**出现时触发 | `subjects` 表必须带 `metadata_json` 且**每条入站都 upsert**（在 processor 里做）；重启后主动推送靠自己的 DB 重建 `ChannelSubject` |
| 12 | `ChannelSubject` 是公开 dataclass，可外部构造（`models.py:311-332`）；`resolve_push_subject` 会合并内存态、平台 backfill，并剥掉 `EPHEMERAL_PUSH_META` | 推送可靠的字段只有 `subject_id` + 持久化白名单 metadata；平台临时路由字段（`msg_id`/`response_url`/`context_token`/`_frame`）不能落库当依赖 |
| 13 | `media_backend` 为 None 时 `start()` 兜底 `FileSystemMediaBackend("/")`，且其读写是**同步** `write_bytes/read_bytes`（`media.py:60-75`） | 必须显式传自己的 media 根；大文件会阻塞 event loop，需 async/`to_thread` 包装 |
| 14 | dict config 走 `ChannelConfig.from_dict`：**静默忽略未知字段**；weixin 的 `accounts` 无法用 dict 表达（必须构造 `WeixinConfig` 对象） | 不能把 DB 的 `config_json` 盲投给 `add_channel`；要自建 per-kind config builder + 独立校验层（否则拼写错误永远不报） |
| 15 | `probe_channel()` 会用临时实例 start/stop 验证凭证且不注册（yuanbao 特判 sign-token） | `imgw channel test` 直接用它，不用自己造 |
| 16 | `on_pre_lock` / `control_message_predicate` / `interrupt_message_predicate` / `run_in_session` 是为宿主预留的 | `/stop`、`/cancel`、定时任务不打断当前 turn 的挂点已有，不必改上游 |
| 17 | `enqueue()` 用 `call_soon_threadsafe`，允许从 SDK 线程投递（`manager.py:249-270`） | 所有通道 SDK 回调线程都能直接进队列；但 `add_channel`/`start` 必须在主 loop 里 await |
| 18 | `octop_gateway.__all__` 里的 `DASHBOARD_CHANNEL_ID` / `DashboardChannel` **未定义** | 上游 bug：`from octop_gateway import *` 会炸。我们自己别用通配导入；顺手上游 issue |

### 11.3 参考实现（Octop / octop-harness）能抄什么

- **Octop 把 gateway 当进程内库用**：`ChannelManager` 是它 uvicorn/FastAPI 进程里的一个对象，**不存在「gateway 服务 ↔ 进程外 agent」的现成契约**。→ 我们的入站投递契约必须自己定义（§11.4 D1）。
- **可抄**：
  - 「控制面也是一个通道」的模板：Octop 的 `WebSocketChannel` / `CliChannel` 作为 `BaseChannel` 子类接入 manager，白拿会话串行、排队、限流（对应我们的 `/v1/events` SSE 面）。
  - `ThreadRegistry` 的会话键格式 `agent:channel_type:subject_id:chat_type` + `sessions`/`threads` 两表 schema。
  - durable 路由字段**白名单**入库（对应 §11.2 #12），以及 `weixin_context_token` 双份（ephemeral 键 + 持久键）。
  - cron/定时任务走 `run_in_session`，避免与入站 turn 交错。
- **不可抄 / 反面教材**：
  - Octop 的 CLI 会**直写 SQLite 且不通知运行中的 server**（配置要等重启才生效）——双写者问题。我们必须坚持「唯一写者是 daemon，CLI 只走 HTTP」。
  - `GlobalProcessor` 与它的 agent（模型路由/slash/HITL/teams）强耦合，没有复用价值。
- **octop-harness** 是 agent runtime 库，**不依赖** gateway；两者之间的 glue 就是 `examples/backends/octop_harness.py`（processor 的实现样板：chunk→MessageEvent 映射、`typing` 首帧、`error_event`+`completed` 收尾、无重试无超时）。

### 11.4 待决策（下一轮讨论的靶子）

**D1（最重要）入站投递模型** —— 这是外壳的核心，三种：

| 模型 | 形态 | 优点 | 代价 |
|---|---|---|---|
| **A. Webhook 派发** | processor = 对后端 `POST /turn` 并流式读回（SSE/NDJSON），逐帧转 `MessageEvent` | 简单、契约小、天然流式、后端可在任意位置 | 后端必须被网关可达；turn 生命周期绑在连接上 |
| **B. 事件总线（拉）** | 入站落库 + `/v1/events` SSE 推送；后端处理完再回灌本 turn 的事件流；processor 退化为「等本 turn 队列」 | 网关成为真中间件；后端可在 NAT 后；可多消费者（CLI tail） | 多出 turn 注册表、超时、孤儿 turn、事件持久化与重放，代码量最大 |
| **C. WebSocket 接后端** | 后端主动连网关，注册为某 tenant 的 handler，双向流 | 双向、可推送、连接即存活 | 要写新协议 + 重连/重放；与「CLI 走 HTTP」的简洁性不一致 |

建议：**v1 取 A**（契约最小、可直接复用 `MessageProcessor`），`/v1/messages` 负责主动推送；B 的 `/v1/events` 先只做「人肉/CLI 观察流」，不做后端消费语义；C 留到确有「后端在 NAT 后 / 需要网关回推」需求时再加。
无论选哪个，必须同时定：`turn_id + seq + session_key` 的帧格式、幂等键（`InboundMessage` 无统一 `msg_id`，只能按通道从 `metadata` 取）、后端超时与取消语义。

> **已定（2026-11-09）：取 A。** 线上契约收敛为**单一 `TurnFrame` 信封**——request body = 帧序列的第一帧（`kind=start`），response = 同构帧序列。详见 §12。

**D2 v1 通道范围**：建议分三档。
- T0 官方机器人协议：feishu、wecom、dingtalk、qq、telegram、discord（有官方 SDK/文档）。
- T1 逆向但可长期跑：weixin（会话过期即重扫码）。
- T2 实验：yuanbao、xiaoyi（私有协议、单连接、硬编码地址）。
v1 只承诺 T0 的「稳定」；T1/T2 标注 experimental，不进 SLA。

**D3 `/readyz` 语义**：因 §11.2 #5，只能定义为「HTTP + SQLite 可用 + enabled 通道已注册」；通道级 `connected` 作为 `status` 的展示字段（尽力而为：discord 公开 API + 其余私有标志 + 最近收发时间），**不进入就绪判定**。

**D4 配置与密钥**：`config_json` 明文落 SQLite（含 app_secret/bot_token）需定策略（建议：DB 文件收紧权限 + 默认不落 secret，secret 走环境变量/独立 `secrets.json`）；并按 §11.2 #14 建 per-kind config builder。

**D5 控制面传输**：Linux 用 UDS（`${XDG_RUNTIME_DIR}/imgw.sock`，天然无端口、天然本机）+ 可选 loopback TCP；Windows 只能 loopback TCP + token。契约层面两者同一套 HTTP，CLI 自动选择。

---

## 12. D1 结论：入站投递 = A，线上契约 = 单一 `TurnFrame` 信封

> 2026-11-09 定。关键设计：**不要 request / response 两套 schema**。一次 turn 的 HTTP 请求就是该 turn 事件流的第一帧（`kind=start`），后端返回的每一帧同构。一个模型、一套校验、一份文档、一张落库日志表。

### 12.1 为什么能「一套结构」

- 上游两个模型本来就镜像：`InboundMessage` 与 `MessageEvent` 共享 `content: list[ContentPart]` + `metadata`（见 §11.2）。
- request 与 response 都是「一次 turn 的事件序列」，区别只是：request 只有 1 帧且 `kind=start`，response 是 N 帧且以 `completed`/`error` 收尾。
- 收益：后端与网关可以共享同一个 pydantic 模型（同一家公司代码）；CLI 的 `/v1/events` 观察流、审计日志、A′ 回灌端点全部复用同一模型。
- 代价：包体是「按 kind 有条件的可选字段」，**必须用 validator 把每个 kind 的必填项钉死**，否则退化成无校验的 JSON 袋子（pydantic 文档里这是反模式，靠纪律补齐）。

### 12.2 模型

```python
class FrameKind(StrEnum):
    START = "start"                  # 网关 → 后端（也是 request body）
    MESSAGE = "message"              # 以下均为 后端 → 网关
    DELTA = "delta"
    THINKING = "thinking"
    THINKING_DELTA = "thinking_delta"
    FLUSH = "flush"
    TYPING = "typing"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    NATIVE = "native"                # 平台专有能力：不透明透传，见 §12.7
    ERROR = "error"                  # 终止帧
    COMPLETED = "completed"          # 终止帧
    # ACCEPTED = "accepted"          # 预留：A′ 异步模式 ack

class ChannelRef(BaseModel):
    channel_id: str                  # 全局路由主键，显式命名，如 feishu:tenant-a:sales
    kind: str                        # octop 的 channel_type
    tenant_id: str | None = None

class SubjectRef(BaseModel):
    subject_id: str
    chat_type: str = ""              # direct / group
    display_name: str = ""

class ChannelCapabilities(BaseModel):
    """目标通道当前能做什么；随 start 帧下发，后端据此选择发送策略。"""
    streaming: Literal["native", "buffered"] = "buffered"
    markdown: Literal["native", "converted", "plain"] = "plain"
    media: dict[str, bool] = Field(default_factory=dict)   # image/video/audio/file
    max_text_len: int = 4000
    native_ops: list[str] = Field(default_factory=list)    # 如 ["feishu.card"]

class NativeOp(BaseModel):
    """平台专有能力的载荷：网关只校验 op，payload 原样交给通道子类。"""
    op: str
    payload: dict[str, Any] = Field(default_factory=dict)

class TurnFrame(BaseModel):
    turn_id: str                     # 网关生成（ULID），每帧都带
    seq: int = 0                     # start=0，其后单调 +1
    kind: FrameKind
    timestamp: float = Field(default_factory=time.time)

    content: list[ContentPart] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)   # 语义按 kind 见 §12.3
    error: str | None = None         # 仅 kind=error；给人看的文案
    native: NativeOp | None = None   # 仅 kind=native；平台专有能力

    # ---- 仅 kind=start（网关 → 后端）----
    channel: ChannelRef | None = None
    subject: SubjectRef | None = None
    session_key: str | None = None   # 必须 == channel.get_debounce_key(msg)
    idempotency_key: str | None = None
    deadline_ms: int | None = None
    capabilities: ChannelCapabilities | None = None
    group_context: GroupContext | None = None
```

> 帧的线上格式：SSE（`data: <TurnFrame JSON>` + 空行），心跳注释帧 `: ping` 每 15s；响应头 `Content-Type: text/event-stream`、`X-Accel-Buffering: no`、禁 gzip。
> `content` 是唯一事实来源，**不要加 `text` 便捷字段**（后端自己拼；本地提供 `frame.text()` 辅助函数）。

### 12.3 每个 kind 的必填项（validator 表）

| kind | 方向 | 必填 | content | 说明 |
|---|---|---|---|---|
| `start` | G→B | `turn_id` `channel` `subject` `session_key` `idempotency_key` `deadline_ms` `capabilities` | 入站多模态；文本消息至少 1 个 TextContent | `seq=0`；`metadata` = 平台原始 metadata（只读） |
| `delta` | B→G | `turn_id` | ≥1 个 TextContent | 累积，`completed`/`flush` 才真正发出 |
| `thinking_delta` | B→G | `turn_id` | ≥1 个 TextContent | `show_thinking=false` 时网关直接丢弃 |
| `thinking` | B→G | `turn_id` | ≥1 个 TextContent | 同上 |
| `message` | B→G | `turn_id` | 非空，可含媒体 | 立即发出（文本+媒体可多条） |
| `flush` | B→G | `turn_id` | — | 立即吐出已累积内容，开始新消息 |
| `typing` | B→G | `turn_id` | — | 网关交给通道原生 typing |
| `native` | B→G | `turn_id` `native.op`，且 `native.op ∈ start.capabilities.native_ops` | — | 平台专有能力，payload 不透明透传；不在白名单 = 编程错误（§12.7） |
| `tool_start` / `tool_end` | B→G | `turn_id` `metadata.tool_name` | — | 受 `show_tool_hints` 控制 |
| `error` | B→G | `turn_id` `error` 非空 | — | **终止帧**；`error` 给用户看（后端负责不泄漏内部信息），详情放 `metadata.detail` 只落日志 |
| `completed` | B→G | `turn_id` | — | **终止帧** |
| `accepted`（预留） | B→G | `turn_id` | — | 仅 A′ 异步模式使用 |

### 12.4 映射（全程字段拷贝，不做业务翻译）

`start` ↔ `InboundMessage`：

| `TurnFrame` | `InboundMessage` |
|---|---|
| `channel.channel_id` / `channel.kind` / `channel.tenant_id` | `channel_id` / `channel_type` / `tenant_id` |
| `subject.*` | `channel_subject`（`ChannelSubject`） |
| `content` / `group_context` / `metadata` / `timestamp` | 同名字段 |
| `session_key` | `channel.get_debounce_key(msg)`（默认 `subject_id`） |
| `idempotency_key` | 由网关按 §11.4 的 per-kind 提取器生成 |

帧 ↔ `MessageEvent`：`delta→MessageEvent.delta(text)`、`thinking_delta→thinking_delta()`、`thinking→thinking()`、`message→MessageEvent(content=...)`、`flush→flush()`、`typing→typing()`、`tool_start/end→tool_start(tool_name)`、`error→error_event(text)`、`completed→completed()`。反方向只有 A′ 回灌时才需要。

### 12.5 明确**不**统一的部分

1. **HTTP 层**：状态码（`200` = 开始流；`4xx/5xx` = 网关侧转 `error` 帧）、`Authorization` 头、SSE 响应头。
2. **终止语义**：SSE 连接结束 ≠ turn 结束。必须见到 `completed`/`error`；EOF 而无终止帧 → 网关合成 `error`（否则上游会丢弃已累积的 delta，§11.2 #3）。
3. **`seq` 单调性**：每帧 +1，网关校验；缺帧/重复 → `error`（A′ replay 依赖它）。
4. **主动推送 `/v1/messages`**：不属于 turn 生命周期，单独一个小模型（复用 `ContentPart` / `SubjectRef` / `ChannelRef`）。硬塞进 `TurnFrame` 会引入没有语义的 kind。

### 12.6 实施顺序（等开工）

1. `core/contracts.py`：`FrameKind` / `ChannelRef` / `SubjectRef` / `TurnFrame` + 每 kind 必填校验 + 与上游模型的互转。
2. `gateway/processor.py`：A 的 httpx SSE 实现（幂等检查 → POST → 逐帧转 `MessageEvent` → 保证恰好一个终止帧）。
3. `core/repos/`：`backends`（tenant→后端）、`inbound_dedup`（幂等键）、`subjects`（+`metadata_json`）。
4. 测试：golden JSON 双向 round-trip；非法 kind 组合必须被 validator 拒绝。

### 12.7 一套 body 能否涵盖所有通道类型？—— 语义能，表现不能（且不该）

答案分三层，混在一起谈会得出错误结论：

| 层 | 例子 | 能否一套 `TurnFrame` 涵盖 | 结论 |
|---|---|---|---|
| **语义层** | 纯文本、markdown、流式增量、思考、tool 状态、媒体、typing、错误 | **能** | 10 种 kind + 5 种 `ContentPart` 就是对 10 个通道的交集归一化 |
| **渲染层** | 飞书 `post(md)`、企微 markdown、QQ markdown（失败降级纯文本）、Telegram markdown→HTML、Discord 原生 md | **不需要涵盖** | 后端只输出文本，渲染是通道自己的事（`_send_text` 分叉） |
| **平台专有能力层** | 飞书 interactive card / 企微模板卡片 / QQ 键盘 / 钉钉 ActionCard / 引用 / 话题 / @ | **不能，也不该塞进来** | 一套 schema 只有两种结局：N 路 union（每接一个平台改一次契约）或塞 metadata 字符串（等于没契约） |

关键证据（上游源码，不是我们的设计缺口）：

- **飞书出站只有 `post(md)` / `image` / `file`，没有任何构造 interactive 卡片的代码**（`feishu.py:610-699`）；`_send_message` 虽透传 `msg_type`，但没有调用方会传 `interactive`。
- **飞书入站卡片被压成文本**：`message_type == "interactive"` → `content_data.get("text") or "[Interactive Card]"`（`feishu.py:1169-1172`），按钮的 action payload **直接丢失**。
- 流式不是通道类型差异，而是**发送策略差异**：后端永远发 `delta`，WeCom/QQ/Telegram 走原生流（reply_stream / 分块 / edit 更新），飞书等通道累积到 `COMPLETED` 一次性发。`TurnFrame` 不需要知道目标通道会不会流。

因此定三条：

1. **语义词汇表只有一套**：10 种语义 kind + 5 种 `ContentPart`。后端必须永远能只用它们表达完整回复——这是降级契约的底线。`native` 是唯一的旁路，payload 不透明、网关不解释，平台结构不进 `content`。
2. **平台专有能力 = 同一个 webhook 里的 `native` 帧，不是第二个 webhook**：
   - 一个后端只实现一个端点 `POST /v1/turns`；加一个平台**不需要重新注册**，通道差异全在 `start.channel` / `start.capabilities` 和帧内容里。
   - `native.payload` 是平台文档原样 JSON（如飞书卡片 schema）。网关只校验 `native.op ∈ capabilities.native_ops`，然后交给对应通道子类的方法执行（`feishu.card` → 我们自己 `FeishuChannel` 子类的 `send_card`）。
   - 在不支持该 op 的通道上发 `native` 帧 = 编程错误：网关回 `error` 帧并记日志。正确姿势是先读 `start.capabilities`。
   - 入站方向的卡片回调是真正缺能力的地方：当前 `parse_inbound` 把 `interactive` 压成文本（`feishu.py:1169-1172`），action payload 丢失；要支持必须子类化 `FeishuChannel` 并增加卡片事件路由，**v1 明确不做**。
3. **能力随 `start` 帧下发**（不额外查询）：`start.capabilities` 给出流式模式、markdown 模式、媒体支持、长度上限、可用 native ops。后端据此决定发 `delta` 还是 `message`、要不要用 native。**capability 不能从上游自动推导**（`ChannelConstraints` 只管限流/超时/显示开关），逐通道人工填并在测试里锁定。`GET /v1/channels/{id}` 可返回同一结构供 CLI/调试。
4. **渲染 ≠ 降级（重要）**：语义层发文本/markdown 后，各通道用自己的原生渲染（飞书 `post(tag=md)`、企微 markdown、QQ markdown 带降级、Telegram→HTML），**通用路径产出的是原生观感，不是「最回退的纯文本」**。只有语义表达不了时才降级成文本/链接（如视频在飞书降级为链接）。

**v1 明确不支持**（登记为限制，不装作支持）：交互卡片（按钮/表单/卡片回调）、引用/话题的主动设置、@人、消息编辑（telegram 的 edit 属于通道内部实现，不对外暴露）、已读回执。要支持其中任一项，都应作为「通道级 native 能力」单独立设计，而不是扩 `TurnFrame`。

---

## 13. 状态清单、存储、控制面 API 与 CLI（定稿）

> **2026-11-09 第四次修订（定稿）**：配置**与**运行数据都进 SQLite 单一权威；JSON 仅作为配置导入/导出格式；不引入文件配置、不引入 reconcile/sync。
> 前几版（配置进 TOML / 进 YAML / 配置运行分离）作废，翻转条件见 §14.3。

### 13.1 状态清单

| 类别 | 内容 | 生命周期 | 存在哪 |
|---|---|---|---|
| **持久状态（唯一权威）** | 声明式配置：`channels` / `backends` / `settings`；运行数据：`messages` / `turn_events` / `subjects` / `inbound_dedup` / `audit` / `schema_migrations` | 重启恢复 | **SQLite 单库** |
| **bootstrap（不可消除的最小例外）** | ① db 文件路径 ② 控制面监听（UDS 路径或 TCP 端口）③ token 文件路径 | 打开 DB 之前必须知道 | 环境变量 / 命令行参数（都有默认值） |
| **进程内存（重启即丢）** | manager 的通道注册表/队列/worker/session 锁；通道内 subject 注册表；群上下文缓冲；各通道 ws 连接与游标；扫码登录会话 | 重建或丢弃 | 内存（不可控的见 §11.2） |
| **不持有 / 只留引用** | agent 的 thread 与对话记忆、媒体二进制 | — | 后端自己存；媒体在 media_dir（DB 只存 key） |

- **网关不是对话状态的权威**：agent 的 thread/记忆在后端；`messages` 只用于复盘/排障/CLI 回看，**任何业务逻辑不得读它续聊**。
- 配置文件只是**导入/导出格式**，不是运行时配置源：`imgw config export > cfg.json` / `imgw config import cfg.json`。

### 13.2 SQLite schema v1

```sql
-- 启动时检查 schema_migrations 自动升级；PRAGMA journal_mode=WAL; auto_vacuum=INCREMENTAL; foreign_keys=ON
-- 单写者（daemon）；备份 VACUUM INTO
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);

CREATE TABLE settings (                      -- 全局 knobs + 保留策略；值用 JSON 编码
  key        TEXT PRIMARY KEY,               -- workers_per_channel / log_level / retention.chat_days / ...
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE channels (                      -- 一行 = 一个运行实例
  channel_id       TEXT PRIMARY KEY,         -- 不可变、全局路由主键，如 feishu:tenant-a:sales
  kind             TEXT NOT NULL,            -- feishu / wecom / qq / ...
  tenant_id        TEXT,                     -- 多租户键，也是 backend 路由键
  alias            TEXT UNIQUE,              -- 可选短名，CLI 用；可变、不影响路由
  name             TEXT NOT NULL DEFAULT '', -- 显示名
  config_json      TEXT NOT NULL,            -- 平台 Config 序列化；secret 只存 ${ENV} 引用
  constraints_json TEXT NOT NULL DEFAULT '{}', -- ChannelConstraints 覆盖（show_* 必须写进 config）
  enabled          INTEGER NOT NULL DEFAULT 1,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL,
  last_started_at  REAL                      -- 最后一次成功注册；诊断崩溃循环
);

CREATE TABLE backends (                      -- 一行 = 一个后端 webhook；默认部署只有一行
  tenant_id   TEXT PRIMARY KEY,              -- '' = 默认后端
  base_url    TEXT NOT NULL,
  token_ref   TEXT NOT NULL,                 -- '$FEISHU...' 形式的环境变量引用
  timeout_ms  INTEGER NOT NULL DEFAULT 120000,
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE subjects (                      -- 出站通讯录：能主动推给谁；每条入站 upsert
  channel_id    TEXT NOT NULL,
  subject_id    TEXT NOT NULL,
  chat_type     TEXT NOT NULL DEFAULT '',
  display_name  TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',  -- 白名单过滤后的 durable 路由字段
  first_seen    REAL NOT NULL,
  last_seen     REAL NOT NULL,
  PRIMARY KEY (channel_id, subject_id)
);

CREATE TABLE inbound_dedup (                 -- 已处理台账：吃掉平台重投
  idempotency_key TEXT PRIMARY KEY,
  channel_id      TEXT NOT NULL,
  created_at      REAL NOT NULL
);

CREATE TABLE audit (                         -- 操作审计；secret 必须脱敏
  id           INTEGER PRIMARY KEY,
  ts           REAL NOT NULL,
  actor        TEXT NOT NULL,                -- api-token:<label> / system / offline-cli
  action       TEXT NOT NULL,                -- channel.add / message.push / config.import / turn.error
  channel_id   TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE messages (                      -- 聊天记录：入站一条 + 出站「最终」一条；不存 delta 碎片
  id              INTEGER PRIMARY KEY,
  channel_id      TEXT NOT NULL,
  subject_id      TEXT NOT NULL,
  session_key     TEXT NOT NULL,
  direction       TEXT NOT NULL,             -- in / out
  turn_id         TEXT,
  kind            TEXT NOT NULL,             -- inbound / message / native / error
  content_json    TEXT NOT NULL,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  platform_msg_id TEXT,
  ts              REAL NOT NULL,
  error           TEXT
);
CREATE INDEX idx_messages_subject ON messages(channel_id, subject_id, ts);
CREATE INDEX idx_messages_ts      ON messages(ts);

CREATE TABLE turn_events (                   -- 可选：原始帧，仅排障 / A′ 回放；默认 1 天
  turn_id      TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  ts           REAL NOT NULL,
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (turn_id, seq)
);
```

字段决定：

- `channel_id` 不可变；`alias`（唯一短名，CLI 解析用）+ `name`（显示名）都可变。
- `created_at` 回答「添加时间」；`last_started_at` 仅诊断。
- 删 channel 默认保留 `subjects`/`messages`；`--purge` 才级联。
- `settings` 里放：`workers_per_channel`、`queue_maxsize`、`log_level`、`turn_deadline_ms`、`error_texts`、`retention.*`。

### 13.3 「全局唯一的 webhook」澄清

- **A 模式下 webhook 是后端的**：后端实现 `POST /v1/turns`；网关不暴露公网 webhook（10 通道全部出站，零公网 IP）。
- **单后端部署 = `backends` 只有 `tenant_id=''` 一行**；多租户时多行，按 `channels.tenant_id` 路由。
- 网关的 HTTP 面是**控制面**（UDS 或 loopback + token），不是平台回调入口。

### 13.4 修改路径、离线模式与备份

**运行时修改**（唯一路径）：

```
CLI ──HTTP──▶ daemon：校验 → 事务写 SQLite → 应用到 manager（单实体已知操作，无全局 diff）
```

- 没有 `sync` 命令、没有 reconcile 引擎：改与生效是同一次调用。
- 单实体变更语义固定：新增 → `add_channel`；删除 → `remove_channel`；`kind/config/tenant` 变 → `remove_channel` + `add_channel`（上游无 update API）；仅 `alias/name` → 原地更新；`enabled` 翻转 → add/remove。

**离线模式**（daemon 不在时，不给第二套存储格式）：

- CLI 检测 `daemon.lock`（PID + 数据库文件锁）：daemon 在运行 → 必须走 HTTP；不在运行 → 允许 `--offline` 直连 SQLite，用 `BEGIN IMMEDIATE` 独占写。
- 离线操作的 actor 记为 `offline-cli`，同样写 `audit`。
- 这条路径同时就是**破窗工具**：daemon 起不来时仍能改配置、禁用坏通道。

**配置导入/导出（JSON）**

- `imgw config export [--with-secrets] > cfg.json`：默认把字面 secret 打码为 `"***"`；`${ENV}` 引用原样输出。
- `imgw config import cfg.json`：同一套 pydantic 校验；打码值表示「保留现值」；整体在一个事务里应用。
- 配套仓库内置 `config.schema.json`（JSON Schema），编辑器可补全、CI 可校验。
- 用途：入 git / 交付备份 / 跨环境迁移 / `imgw config validate` 离线检查。

**备份**

- `imgw db backup <path>` → `VACUUM INTO`（在线、一致快照）。
- 恢复 = 停 daemon → 替换 db 文件 → 启动（自动跑迁移）。
- SQLite 运维三条：WAL；单写者；禁止网络盘。

### 13.5 密钥与 bootstrap

**bootstrap（只有这三项，均可给默认值）**

| 项 | 来源 | 默认 |
|---|---|---|
| db 路径 | `IMGW_DB` / `--db` | 平台数据目录 |
| 控制面监听 | `IMGW_LISTEN` / `--listen` | POSIX：UDS `$XDG_RUNTIME_DIR/imgw.sock`；Windows：`127.0.0.1:8765` |
| token 文件 | `IMGW_TOKEN_FILE` / `--token-file` | `~/.config/imgw/token`（首次启动生成 0600） |

**密钥**

- DB 里存 `"${FEISHU_APP_SECRET}"` 这类**环境变量引用**，daemon 构造通道时在内存展开；**写回/导出绝不写展开后的明文**。
- 也允许字面量（扫码登录得到的 token 只能字面量），但 `imgw doctor` 会对字面量告警。
- 不进日志、不进 `audit.payload_json`、`/v1/status` 只回 `has_secret: true`。
- DB 文件 0600、目录 0700；备份文件同样收紧权限或加密。

### 13.6 控制面 API（CLI 的唯一入口）

| Method + Path | 用途 | 副作用 |
|---|---|---|
| `GET /healthz` | 进程存活（给 systemd） | — |
| `GET /readyz` | HTTP + SQLite + enabled 通道已注册（**不含 connected**，§11.2 #5） | — |
| `GET /v1/status` | 版本、uptime、每通道状态与 capability、队列深度（best-effort）、配置修订号 | — |
| `GET/POST /v1/channels` | 列表 / 新增（先 probe 后 register，事务写 DB） | DB + manager |
| `GET/PATCH/DELETE /v1/channels/{ref}` | 详情（含**脱敏** config + credentials 摘要）/ 改 alias·name·enabled·config（`"***"` 表示保留原值，部分更新）/ 删除（**要求已 logout**，否则 409） | DB + manager |
| `POST /v1/channels/probe` | 凭证探测，不注册 | `manager.probe_channel` |
| `POST /v1/channels/{ref}/login` | 扫码登录（飞书/企微/钉钉/QQ/微信；返回 qr 文本/URL），确认后写 DB + 自动启用并注册 | QR 助手 + DB |
| `GET /v1/channels/{ref}/subjects` | 已知推送目标（DB 与内存合并） | — |
| `POST /v1/messages` | 主动推送（`Idempotency-Key` 必填） | `manager.push_*` + audit |
| `POST /v1/messages/broadcast` | 向某通道全部 subject 推送 | `manager.push_to_all` |
| `GET /v1/events` | SSE 观察流 | 只读 |
| `GET /v1/history` | 聊天记录查询 | 只读 `messages` |
| `GET /v1/config/export` / `POST /v1/config/import` | 配置导出（默认脱敏）/ 导入（事务 + 校验） | DB |
| `POST /v1/db/maintenance` | 保留策略清理 / `incremental_vacuum` | DB 维护 |
| `GET /v1/version` | 网关与 octop-gateway 版本 | — |

### 13.7 CLI 命令 → API → 状态/上游调用

| 命令 | 作用 | 调控制面 | 触碰状态 / 上游 |
|---|---|---|---|
| `imgw run` | **唯一生产启动进程**：迁移 → 读 DB → reconcile channels → 起 uvicorn | 无（进程本身） | 全量 |
| `imgw run-dev [--dev-backend-port 9900]` | 开发模式：同进程拉起回显后端，所有 tenant 走它（**不写 backends 行**），其余机制（messages/subjects/dedup/audit）照旧 | 无（进程本身） | 全量 |
| `imgw install-service [--dry-run] [--print-only] [--user/--system] [--workdir]` | 生成并安装 systemd / launchd / NSSM 定义（`--print-only` 只打印，`--dry-run` 不写不执行） | 无 | 服务定义文件 + 系统命令 |
| `imgw uninstall-service` / `start [--wait]` / `stop` / `restart` | 生命周期转发给系统服务管理器；`start --wait` 轮询 `/readyz` | `GET /readyz`（仅 `--wait`） | 系统服务 |
| `imgw start [--wait]` / `stop` / `restart` | 转发给系统服务管理器；`--wait` 轮询 `/readyz` | `GET /readyz` | 系统服务 |
| `imgw status [--json]` | 守护进程 + 每通道状态 | `GET /v1/status` | 内存 + DB |
| `imgw channel login <target> [--id --name --tenant --config --app-id/--app-secret/--bot-id/--secret/--app-key/--token/--account-id/--base-url --secret-stdin --rescan --check --format --json --qr-png --timeout --interval --no-wait --session-id]` | 绑定/重登：kind 建号、无凭据扫码、有凭据重连、凭据参数静默/覆盖、`--rescan` 强制扫码；`--format both|url|qr|json` 控制输出（stdout 数据、stderr 提示） | `GET /v1/channels/{ref}` → `POST /v1/channels` / `PATCH /v1/channels/{ref}` → `POST /v1/channels/{ref}/login` + 轮询 | DB + manager + QR 助手 |
| `imgw channel logout <ref>` | 停用（=旧 disable），保留凭据、subjects/messages；幂等 | `PATCH /v1/channels/{ref}` `{"enabled": false}` | DB + manager |
| `imgw channel list [<ref>] [--json] [--header]` | 无参：TSV 列表（channel_id/kind/state/creds/alias）；带 ref：详情（含脱敏 config）；secret 永不输出 | `GET /v1/channels` / `GET /v1/channels/{ref}` | DB + manager |
| `imgw channel delete <ref> [--purge] [--yes]` | 删行；**要求先 logout**（否则 409）；`--purge` 级联 subjects/messages | `DELETE /v1/channels/{ref}` | DB + `manager.remove_channel` |
| `imgw channel test <kind> --config …` | 凭证探测 | `POST /v1/channels/probe` | `manager.probe_channel` |
| `imgw subject ls <ref>` | 已知推送目标 | `GET /v1/channels/{ref}/subjects` | DB + 内存 |
| `imgw send <ref> <subject> <text>` / `--all` | 主动推送 / 广播 | `POST /v1/messages[/broadcast]` | `manager.push_*` + audit |
| `imgw events [--channel …]` | tail 事件流（SSE） | `GET /v1/events` | 只读 |
| `imgw history [--channel --subject --since --limit]` | 查聊天记录 | `GET /v1/history` | `messages` |
| `imgw config export [--with-secrets] [-o f.json]` | 导出配置（默认脱敏） | `GET /v1/config/export`（离线可） | DB |
| `imgw config import <f.json>` | 校验并事务导入 | `POST /v1/config/import`（离线可） | DB + manager |
| `imgw config validate <f.json>` | 纯本地校验（CI/pre-commit） | 无（本地 pydantic） | JSON 文件 |
| `imgw db backup <path>` / `vacuum` / `migrate` | 备份 / 压缩 / 迁移 | `POST /v1/db/maintenance` 或离线 | SQLite |
| `imgw doctor` | 自检：DB 完整、media 可写、backend 可达、凭证 probe、字面 secret 告警 | 组合调用 | 只读 + probe |
| `imgw version` | 本地 CLI 与 daemon 版本 | `GET /v1/version` | — |

### 13.8 铁律

1. **SQLite 是唯一权威；daemon 是唯一在线写者；CLI 是唯一修改入口**（离线模式 CLI 独占写 DB，属受控例外）。
2. **没有 `sync`、没有配置文件 watch、没有 reconcile 引擎**：改与生效同一次调用。
3. **只有 `imgw run` 启动进程**；`install-service` 只生成/安装系统服务定义（会 `enable --now` 交给系统服务管理器拉起 `imgw run`）；其余命令是 HTTP 客户端（离线模式除外）。
4. `channel add --check` / `channel test` 走 `probe_channel`，避免坏凭证注册出半死通道。
5. `enable/disable` 保留配置与数据；`rm --purge` 才级联清理。
6. `send` 必须带 `Idempotency-Key`；失败进审计/重试，但**不自动重试 turn**。
7. 所有写操作落 `audit`；secret 不落日志、不落 audit、`/v1/status` 只回 `has_secret: true`。
8. 配置导入是**单事务**：校验失败整体不生效，绝不半应用。

### 13.9 聊天记录与保留策略

**写入时机**

- 入站：processor 拿到 `InboundMessage` 后**先落 `messages(direction='in')`**，再调后端 → 后端失败/超时也有记录。
- 出站：只落**最终发给用户的内容**（`message/native/error`），不落 delta 碎片；原始帧进 `turn_events`（默认 1 天）。
- 送达状态 best-effort（上游 push 无返回值且行为不一，§11.2 #4）：记 `delivery=unknown|ok|failed`。

**保留策略**（`settings.retention`；后台任务每天跑，启动补跑）

| 键 | 默认 | 说明 |
|---|---|---|
| `chat_days` | 30 | 聊天记录按时间删（主策略） |
| `chat_max_rows` | 500000 | 总行数兜底 |
| `turn_events_days` | 1 | 原始帧；`0` = 不落库 |
| `dedup_days` | 7 | 幂等键，必须短于平台重投窗口 |
| `audit_days` | 90 | 审计单独保留 |
| `media_days` | 30 | media_dir 按 mtime 清理，与 messages 独立 |

**实现要点**

- 分批删（`LIMIT 5000` 循环），避免长事务锁库；`auto_vacuum=INCREMENTAL` + 定期 `incremental_vacuum`。
- 媒体 GC 只看 media_dir 的 mtime/引用，不依赖聊天记录。
- 隐私：`messages` 含用户原文，DB 0600；`imgw history` 走 token，并审计「谁查了历史」。

### 13.10 开发模式：dev-backend（调试套件）

- 源码套件：`src/im_gateway/dev/backend.py`，暴露 `create_dev_backend_app(token="")` 与 `python -m im_gateway.dev.backend --port 9900`。
- 契约：`POST /v1/turns` 接收 `kind=start` 帧，SSE 返回 **一帧 `message` + 一帧 `completed`**；`message` 用固定文本一次性返回 `channel_id / channel_kind / tenant_id / subject_id / subject_name / chat_type / session_key / turn_id / idempotency_key / message`，方便通道开发者对照“网关实际收到了什么”。
- 两种用法：
  1. `imgw run-dev [--dev-backend-port 9900]`：进程内起 uvicorn 线程，`BackendRouter(override=...)` 让**所有 tenant** 都路由到它，不写 `backends` 表；控制面退出时一起停；
  2. 独立跑：`uv run python -m im_gateway.dev.backend`，按普通 backend 注册（可带 `--token`）。
- 仅用于调试：不写 backends 配置、不做真实业务；独立子命令 `run-dev` 即显式开关，生产入口仍是 `run`。

---

## 14. 决策定稿（对齐清单）

### 14.1 最终决定

| # | 决策点 | 结论 |
|---|---|---|
| D-01 | 形态 | 单包两面具：`imgw run` 起 uvicorn + FastAPI + ChannelManager（同 loop）；CLI 只做 HTTP 客户端；不自 daemonize；`install-service` 生成 systemd/launchd/NSSM |
| D-02 | 入站投递 | **模型 A**（webhook 流式派发）：后端实现唯一 `POST /v1/turns`；网关 → 后端；SSE 流；必须是异步生成器 |
| D-03 | 线上契约 | **单一 `TurnFrame` 信封**：request body = `kind=start` 首帧；response = 同构帧序列；11 种 kind（含 `native`）；`capabilities` 随 `start` 下发；必须有终止帧（§12） |
| D-04 | 配置存储 | **SQLite**（channels/backends/settings）；CLI→API→DB 即时生效；无 sync/无文件配置 |
| D-05 | 运行数据 | **同一 SQLite**：messages/turn_events/subjects/inbound_dedup/audit/schema_migrations |
| D-06 | 配置文件 | 仅作 **JSON 导入/导出**（`config export/import`）+ JSON Schema 校验；不是运行时配置源 |
| D-07 | 密钥 | DB 存 `${ENV}` 引用，内存展开；写回/导出永远保留占位符；字面量仅限扫码所得 token 并告警 |
| D-08 | bootstrap | 仅三项：db 路径、控制面监听、token 文件（env/参数，均有默认） |
| D-09 | 控制面 | UDS（POSIX）/ loopback TCP（Windows）+ token；仅 CLI 与主动推送使用 |
| D-10 | 健康语义 | `/healthz` 进程；`/readyz`=HTTP+DB+enabled 通道已注册，**不承诺 connected** |
| D-11 | 生命周期 | 启动从 DB reconcile；单通道失败不阻断；配置变更=remove+add；shutdown 自己实现（停收→等 in-flight≤N→stop→关 DB） |
| D-12 | 多节点 | v1 单进程；四 seam（ChannelRepo→PG / LeaseStore→Redis / Ownership / CursorStore）；配置在 DB 天然可切 PG |
| D-13 | CLI | 见 §13.7；无 `sync`、无 `reload`；`config export/import/validate` + `db backup/vacuum/migrate` |
| D-14 | 依赖 | pin `octop-gateway==1.0.3`；Python ≥3.12（建议 uv 固定 3.12 对齐上游 CI）；AGPL-3.0 + `THIRD_PARTY_NOTICES.md` |
| D-15 | 明确不做 | 交互卡片/卡片回调、引用/@ 主动设置、消息编辑、多消费者事件总线、网关持有对话记忆、MQTT 通用接入；weixin/yuanbao/xiaoyi 标 experimental 不进 SLA |

### 14.2 相对交接文档前几版被取代的内容

| 原位置 | 原结论 | 现在 |
|---|---|---|
| §5.4 | 数据模型（channels/subjects/idempotency/audit） | 由 §13.2 取代（新增 backends/settings/messages/turn_events/migrations） |
| §5.6 | 控制 API 含 `/v1/reload`；CLI 与 daemon 通过 HTTP | 改为无 reload（§13.6） |
| §7 #1/#2/#3 | 待确认 | 已关闭（§11.1） |
| §7 #5/#6/#7 | CLA/多节点协调/双许可 | 仍未决，不影响 v1 |
| §11.4 D1 | 待决策 | 已定 A（§12） |
| §11.4 D4 | 配置与密钥未定 | 已定（§13.5） |

### 14.3 翻转条件（记录备查）

- **配置改回文件（YAML/JSON）** 的条件：K8s/ConfigMap 部署；多人运维需要 git PR 审批；配置必须随代码发布整机重建；交付方要求不依赖 CLI 即可改配置。此时需补齐 reconcile/reload 与双写者处理，并接受多一个子系统。
- **格式若必须选文件**：选 JSON（严格、确定性序列化、全链路一致）；YAML 仅在「人写、机器只读、需要注释」时不劣。
- **运行数据放文件** 任何时候都不建议：`subjects`/`inbound_dedup` 是每条消息都写的状态，放进可手工编辑的文件等于把配置变成没有索引和事务的数据库。

### 14.4 仍未决（不阻塞 v1）

1. CLA 还是 DCO（影响能否商业双许可）——尽早定。
2. 多节点协调层 Redis vs etcd（v2）。
3. 是否商业双许可。
4. 二维码终端兼容（Windows Terminal / 深色主题）——用 `segno` 实测。
5. 上游 `octop_gateway.__all__` 引用未定义符号（`DASHBOARD_CHANNEL_ID` / `DashboardChannel`）——是否提 issue。

---

## 15. 实施状态（2026-11-09：离线可完成部分全部完成）

> 验收口径：**只认单元测试**（`uv run pytest`），不把冒烟测试当作验收依据。
> 所有测试均为离线测试：`httpx.MockTransport` 假后端、fake manager/runner/QR adapter、`TestClient`；不连接任何真实平台、不启动 systemd、不发真实网络请求。

### 15.1 完成清单

| 模块 | 内容 |
|---|---|
| 工程 | `pyproject.toml`（uv + hatchling，pin `octop-gateway==1.0.3`，pytest/ruff/mypy/pytest-cov 配置） |
| `core/` | `contracts.py`（`TurnFrame` 11 种帧 + 逐帧校验 + 上游互转 + metadata JSON 清洗）、`config.py`（`ConfigDocument`）、`capabilities.py`（10 通道初始 capability 表）、`paths.py`（bootstrap 三项 + 0600 token） |
| `db/` | 9 张表（§13.2）+ `Store`（短连接/线程安全、迁移、分批 prune、`VACUUM INTO` 备份、幂等 claim） |
| `gateway/` | `app.py`（§13.6 全部路由 + 鉴权 + 错误映射 + SSE）、`runtime.py`（Store↔ChannelManager 接线、状态机、config import、优雅停机）、`factory.py`（per-kind config，weixin 特判）、`events.py`（进程内 SSE 总线）、`processor.py`（**模型 A 实现**）、`idempotency.py`（8 通道 msg id 提取 + hash 兜底）、`maintenance.py`（retention 调度）、`login.py`（扫码登录状态机 + QQ/微信 adapter）、`dev_processor.py`（echo 占位，仅测试/自检用） |
| `cli/` | 17 个命令（新增 `channel login`、`run-dev`），httpx 客户端（UDS/TCP、token、SSE）、segno 二维码；`service.py`（systemd/launchd/NSSM 生成 + 生命周期命令） |
| 文档 | `README.md`、本文件 §12~§15 |

### 15.2 已接通的关键行为（都有单测钉住）

1. **模型 A processor**：幂等 claim → 入站消息落库 → `subjects` upsert（过滤临时字段）→ 按 tenant 选后端（`${ENV}` 展开）→ 构造 `start` 帧（含 capability）→ SSE 逐帧解析 + `seq` 严格 +1 校验 → 转 `MessageEvent`；**恰好一个终止事件**；所有异常在流内转成中文 `ERROR`，`CancelledError` 透传；上游 `aclose` 早停时 HTTP client 确定性关闭。
2. **metadata 清洗**：WeCom 等适配器会把 live SDK 对象（`_frame` / `_ws_client`）放进 `InboundMessage.metadata`；网关在写 wire body 与 SQLite 前统一剔除，避免正常消息退化成内部错误（回归测试见 `tests/test_processor.py` + `tests/test_contracts.py`）。
3. **落库**：入站一条 + 出站最终内容一条（DELTA 合并，不落 token 碎片）；`subjects` 每条入站刷新；`inbound_dedup` 拦截平台重投；`audit` 记录管理动作。
4. **优雅停机**：`settings.shutdown_grace_ms`（默认 10s）内等待 `active_turns` 归零，再 `manager.stop()`。
5. **retention**：启动补跑 + 每日循环；`settings.retention` 覆盖默认；单次失败不杀循环。
6. **扫码/静默登录**：内存会话状态机（pending/scanned/confirmed/expired/取消，TTL）+ 5 平台 adapter（飞书 `lark_oapi.register_app`、企微 `ai/qc`、钉钉 device flow、QQ/微信上游 helper；均可注入 fake）+ 凭证合并写回 channel config（`"***"` 表示保留原值；weixin accounts 按 `account_id` 合并）+ **登录成功自动启用** + 审计 + 事件发布；CLI `login` 另有静默凭据、重连、`--rescan`、`--format url|qr|json|both`、`--qr-png`、`--no-wait/--session-id`。
7. **系统服务**：生成 systemd unit / launchd plist / NSSM 命令；`install-service`（默认用户级，`--system` 需 root；`--print-only` 只打印；`--dry-run` 不写不执行）、`uninstall-service`、`start --wait`（轮询 `/readyz`）、`stop`、`restart`；runner 可注入，失败统一 `ServiceError` → exit 1。

### 15.3 未完成（依赖真实平台/外部环境，需要真机验证）

1. 5 个平台扫码登录的真实上游网络调用（飞书/企微/钉钉/QQ/微信 adapter 与状态机已测，真实平台流程未跑；`lark_oapi.register_app` 的 addons 事件预置效果待真机确认）。
2. 10 个通道的真实连接与收发（需要真实凭证与网络）。
3. capability 表逐通道真机校准（§12.7 的初始值未经实测）。
4. outbox / 推送重试与 delivery 状态（需要真实通道的失败语义验证）。
5. `imgw doctor` 的 backend 可达性检查（需要真实后端服务）。
6. 多节点 seam 实现（v2）；CLA/DCO、协调层选型等未决项（§14.4）。
7. 登录会话是进程内存态：daemon 重启后未完成的扫码会话丢失（重新 `imgw channel login`）。

### 15.4 测试与覆盖率

```
uv run pytest -q                        451 passed
uv run ruff check src tests             All checks passed
uv run mypy                             Success: no issues found (25 source files, strict)
uv run pytest --cov=im_gateway           TOTAL 3400 stmts, 125 miss, 96%
# 未覆盖分支主要是 feishu/wecom/dingtalk 三个扫码 adapter 的真实 HTTP/线程调用；
# 离线单测覆盖了全部状态机与错误映射，平台网络分支由真机冒烟验证。
```

100% 覆盖的模块：`core/config`、`core/project paths`、`db/models`、`db/schema`、`gateway/app`、`gateway/runtime`、`gateway/events`、`gateway/factory`、`gateway/idempotency`、`cli/client`、`cli/main`、`cli/qr`。
剩余未覆盖行全部是平台分支或并发竞态防御（Windows/POSIX 另一侧、SQLite 唯一约束竞态、`# pragma: no cover` 的不可达兜底），不包含业务逻辑。

> 冒烟记录（仅供参考，不作为验收）：真实 daemon 曾跑通 `imgw run` → `/healthz` `/readyz` → `status` / `channel add` / `config export|import` / 重启（迁移幂等）/ `channel rm`。

### 15.5 与设计文档的实现偏差（已确认可接受）

1. `config import` 是一次性批量应用（先全量校验再写库），不是常驻 reconcile。
2. `POST /v1/messages` 幂等键在发送**前** claim，发送失败不释放；CLI 每次生成新 key，不受影响（outbox 时一并修）。
3. `readyz` 只检查 enabled 通道 `state == running`，不检查上游 `connected`（上游无统一状态 API）。
4. Windows 上 `uv` 解析到的运行时是 **Python 3.12**（uv-managed），与上游 CI 对齐。
5. `POST /v1/channels` 返回 200（不是 201）。
6. `install-service` 会执行 `systemctl enable --now`（即顺带启动服务），不再只是写文件；`--print-only` 才是纯生成。
7. `start/stop/restart/uninstall-service` 默认用户级（`--user`），`--system` 需要 root。
8. `imgw channel login` 的二维码：`--format both|url|qr|json` 控制 stdout（`url` 只有一行 URL，`json` 为 NDJSON event），终端二维码由 CLI 用 `segno` 渲染，`--qr-png PATH` 另写 PNG（GUI/手机扫码用）；网关只返回挑战内容。

### 15.6 下一步（需要真实环境）

1. 飞书单通道真机端到端：扫码/凭据 → 私聊 + 群聊 → 流式回复 → 主动推送；据此校准 capability。
2. 企微（原生流 + 10s 占位）与 QQ；再 weixin（会话过期重扫码）。
3. outbox / delivery 状态 / backend 可达性 doctor。
4. 双模式 CI 占位（1 节点 SQLite / 3 节点 PG+Redis），为 v2 留出测试结构。
