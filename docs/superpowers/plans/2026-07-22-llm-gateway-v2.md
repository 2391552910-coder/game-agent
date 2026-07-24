# LLM Gateway V2 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有 Gateway v1 通信的前提下，为 myAgent2 增加可持久恢复、严格排序、双向幂等的 LLM Gateway HTTP v2 完整闭环。

**Architecture:** 保留现有 `POST /api/gateway/events` 作为 v1 入口，新增 `POST /api/gateway/v2/events` 和 `GET /api/gateway/v2/capabilities`，Swagger tag 使用 `gateway-v2`。v2 入站事件、控制周期、decision outbox 和 skill call 终态以 PostgreSQL 为唯一事实源，短事务完成 ACK、状态迁移和任务 claim；后台 worker 在事务外执行 Agent 和 HTTP，再以稳定 ID 回写状态。Redis 可用于通知，但不能决定 ACK、顺序或恢复结果。

**Tech Stack:** Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy AsyncSession/raw SQL、PostgreSQL JSONB、Alembic、httpx、pytest/pytest-asyncio、Ruff。

---

## 0. 已确定的设计决策

### 0.1 HTTP 路径

| 功能            | 路径                                 | 说明                                          |
| --------------- | ------------------------------------ | --------------------------------------------- |
| v1 events       | `POST /api/gateway/events`         | 保持现状，不改变既有调用方。                  |
| v2 capabilities | `GET /api/gateway/v2/capabilities` | 只有全部 v2 前置条件通过时才返回能力。        |
| v2 events       | `POST /api/gateway/v2/events`      | 独立 schema、持久化和 worker。                |
| liveness        | `GET /health`                      | 只表示进程存活。                              |
| readiness       | `GET /ready`                       | 检查数据库迁移、worker、Embedding 和 Rerank。 |

不使用 `/api/gateway/events2`。版本属于资源路径层级，应使用 `/api/gateway/v2/events`。

该路径是双方协议变更，不是 myAgent2 可以单方面发布的实现细节。开始生产代码修改前，必须由 Gateway 维护方确认以下三项并形成同一份协议基线：

1. capabilities discovery 使用 `GET /api/gateway/v2/capabilities`，该只读接口不要求 HMAC；未 ready 时返回 503。
2. v2 runtime events 使用 `POST /api/gateway/v2/events`，必须 HMAC 认证。
3. Gateway 接受 `receiveEventsPath=/api/gateway/v2/events`，不再按旧 remediation 固定值拒绝 provider。

未取得确认时，只能编写 fixture/contract 测试，不能修改正式路由或对外返回 v2 capabilities。

`docs/llm-v2-remediation.md` 中 `LLM-V2-001` 的固定路径需要同步修改为：

```json
{
  "contractVersion": "llm-gateway-http-v2",
  "receiveEventsPath": "/api/gateway/v2/events"
}
```

Gateway 侧 provider origin 配置为 `http://<myagent-host>:8000`，capabilities/events 分别使用绝对 path `/api/gateway/v2/capabilities` 和 `/api/gateway/v2/events`；若 Gateway 的 base URL 配置本身带 path prefix，Task 0 必须先冻结它与相对路径的拼接规则，禁止形成重复 `/api/gateway/v2/api/gateway/v2`。

### 0.2 兼容边界

- v1 和 v2 只共享稳定的 HMAC/canonical JSON 工具，不共享 Pydantic envelope、队列或事件状态机。
- v1 继续使用当前 Redis Stream worker，直到 v1 调用方迁移完成。
- v2 不能从 v1 缺失的字段推导或伪造 `controlGeneration`、`eventSequence`。
- `LLM_GATEWAY_V1_ENABLED` 与 `LLM_GATEWAY_V2_ENABLED` 独立控制，禁止自动版本回退。
- v2 capabilities 不能因为路由存在就返回成功；数据库 revision、身份映射、worker 和外部依赖都必须 ready。

### 0.3 事务边界

1. 入站请求先完成 Content-Type、HMAC、时间戳、整批 schema、tenant、batch 内重复和已存在 eventId 内容冲突预检。
2. PostgreSQL 外层事务为每个待接纳事件建立 savepoint；单项可恢复写入失败只回滚该 savepoint，其他成功项可以提交。
3. 并发 eventId 使用 `INSERT ... ON CONFLICT DO NOTHING RETURNING` 后读取已提交 hash 分类；并发内容冲突会回滚整个外层事务并返回 409，不能在冲突响应前留下新接纳事件。
4. 外层事务提交后才返回 `receivedEventIds`/`duplicateEventIds`。未列入两个数组的单项失败事件由 Gateway 重投；整库连接/commit 失败时返回 503 且不 ACK 任何新事件。
5. worker 只在短事务中 claim/更新状态，不在数据库事务中调用 LLM、RAG 或 HTTP。
6. event worker 在 durable decision 已写入 outbox 后即可完成 lease-bearing event；decision HTTP 由 outbox worker独立恢复。

### 0.4 状态机不变量

- 事件幂等键为 `(gatewayId, eventId)`。
- 事件内容 hash 只覆盖 `gatewayId + immutable event`，排除 traceId、requestId、sentAtMs、签名头。
- 排序分区为 `(gatewayId, sessionId, controlGeneration)`。
- `(gatewayId, sessionId)` 另有唯一 session runtime 行，保存 current generation 和单调 `fenceVersion`；claim 和完成都必须 CAS 校验该 fence。
- 每个分区只处理 `eventSequence == nextEventSequence`；gap 不得越过。
- 只有 `session_started(eventSequence=1)` 可以激活 control cycle；其它 sequence=1 事件进入 manual，不运行 Agent。
- 新 generation 的 `session_started(sequence=1)` 生效后，旧 generation 不能再生成 decision。
- 一个 source event 最多生成一个 decision。
- 一个 `decisionId` 永远只对应一个 request body hash。
- 一个 `skillCallId` 只接受一个逻辑终态；重复终态只做幂等合并。
- HTTP accepted/rejected 不产生新 lease；只有 lease-bearing event 可以产生下一次 decision。

### 0.5 当前工作区保护

当前仓库有未提交的 v1、E2E skill 和配置改动。实施时必须：

- 不回滚或覆盖现有改动。
- 每次编辑前重新读取目标文件和 `git diff -- <file>`。
- 不执行 `git reset --hard`、`git checkout --` 或批量格式化无关文件。
- 本计划不要求自动提交；只有用户明确要求提交时，才按文件和 diff 审核后提交。

---

## 1. 文件结构

### 新增生产文件

| 文件                                                           | 职责                                                                          |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `src/api/routes/gateway_v2.py`                               | v2 capabilities/events/readiness HTTP 路由，只负责 transport 和调用 service。 |
| `src/core/integration/llm_gateway_v2/__init__.py`            | v2 integration package。                                                      |
| `src/core/integration/llm_gateway_v2/contracts.py`           | 六类事件 discriminated union、ACK、capabilities、decision request/response。  |
| `src/core/integration/llm_gateway_v2/auth.py`                | 入站/出站 HMAC、AppId 与 gatewayId 绑定、稳定错误。                           |
| `src/core/integration/llm_gateway_v2/canonical.py`           | canonical JSON 和 event/decision body hash。                                  |
| `src/core/integration/llm_gateway_v2/inbox_repository.py`    | session runtime、control cycle、event admission/claim 的 PostgreSQL 短事务。  |
| `src/core/integration/llm_gateway_v2/outbox_repository.py`   | decision plan/claim/HTTP result 的 PostgreSQL 短事务。                        |
| `src/core/integration/llm_gateway_v2/terminal_repository.py` | skill call、唯一终态和 terminal effect 的 PostgreSQL 短事务。                 |
| `src/core/integration/llm_gateway_v2/event_service.py`       | 整批预检、durable ACK 和六类事件业务分派。                                    |
| `src/core/integration/llm_gateway_v2/event_worker.py`        | partition claim、gap、retry、dead-letter、重启恢复。                          |
| `src/core/integration/llm_gateway_v2/decision_service.py`    | Agent 输出收窄、稳定 decision 生成和 outbox 写入。                            |
| `src/core/integration/llm_gateway_v2/decision_client.py`     | 短超时 HTTP、body-first response 校验和安全错误分类。                         |
| `src/core/integration/llm_gateway_v2/decision_worker.py`     | decision outbox 发送、重试、accepted/rejected 合并。                          |
| `src/core/integration/llm_gateway_v2/readiness.py`           | 数据库 revision、worker、Embedding/Rerank readiness。                         |
| `src/core/integration/llm_gateway_v2/worker_status.py`       | event/outbox worker heartbeat、drain 和健康快照。                             |
| `src/core/integration/llm_gateway_v2/worker_hooks.py`        | 默认 no-op、仅由恢复测试注入 barrier 的 typed hooks。                         |
| `src/core/agents/gateway_v2.py`                              | v2 专用 Agent 输入、无副作用决策图、错误提升和能力约束。                      |
| `src/core/agents/gateway_v2_models.py`                       | v2 独立 action/context model，不改变 v1`RecommendedAction`。                |
| `src/core/agents/gateway_v2_prompts.py`                      | v2 动态 skills、lease scope 和`actionId` prompt。                           |
| `alembic/versions/008_llm_gateway_v2_inbox.py`               | session runtime、control cycle 和 durable inbox。                             |
| `alembic/versions/009_llm_gateway_v2_outbox.py`              | decisions 和 skill calls。                                                    |

### 新增测试文件

| 文件                                                   | 职责                                                       |
| ------------------------------------------------------ | ---------------------------------------------------------- |
| `tests/unit/llm_gateway_v2/test_contracts.py`        | 六类事件合法/非法 schema。                                 |
| `tests/unit/llm_gateway_v2/test_auth.py`             | 双向身份、HMAC、gateway 绑定、脱敏错误。                   |
| `tests/unit/llm_gateway_v2/test_canonical.py`        | transport metadata 不参与 event hash。                     |
| `tests/unit/llm_gateway_v2/test_event_service.py`    | ACK、duplicate、conflict、tenant fail-closed。             |
| `tests/unit/llm_gateway_v2/test_event_worker.py`     | sequence、gap、generation、retry/dead-letter。             |
| `tests/unit/llm_gateway_v2/test_decision_service.py` | skills/lease 收窄、稳定 decision/body。                    |
| `tests/unit/llm_gateway_v2/test_decision_client.py`  | non-2xx rejected、unknown reason、非法响应。               |
| `tests/unit/llm_gateway_v2/test_decision_worker.py`  | outbox retry、乱序 accepted/event 合并。                   |
| `tests/unit/llm_gateway_v2/test_terminal_state.py`   | success/failed/cancelled/timeout 和唯一终态。              |
| `tests/api/test_gateway_v2.py`                       | capabilities、events、ACK 和 OpenAPI。                     |
| `tests/api/test_readiness.py`                        | readiness 依赖矩阵。                                       |
| `tests/integration/test_gateway_v2_migrations.py`    | migration、约束和索引。                                    |
| `tests/integration/test_gateway_v2_recovery.py`      | PostgreSQL 上真实 claim、并发、重启恢复。                  |
| `tests/integration/conftest.py`                      | integration tree 共享的`myagent_test_*` 数据库安全门禁。 |

### 新增验证脚本

| 文件                                       | 职责                                                                          |
| ------------------------------------------ | ----------------------------------------------------------------------------- |
| `scripts/seed_gateway_v2_test_tenant.py` | 在显式测试数据库创建 E2E 假 tenant/identity，不输出 secret。                  |
| `scripts/invoke_gateway_v2_e2e.py`       | 签名调用 account-login-start/status/metrics，只驱动真实 Gateway，不伪造事件。 |
| `scripts/assert_gateway_v2_state.py`     | 只读校验 inbox/outbox/call/cycle/fence 和 raw body hash。                     |

### 修改文件

- `src/api/main.py`：注册 v2 router，启动/停止两个 v2 worker，拆分 liveness/readiness。
- `src/config.py`：增加 v1/v2 feature flags、限制、retry 和身份绑定配置。
- `src/core/infrastructure/db.py`：关闭默认 SQL echo。
- `src/core/agents/nodes.py`、`decision_nodes.py`、`orchestrator.py`：只做日志脱敏和错误分类；不改变 v1 action schema。
- `.env.example`：只增加变量形状和假 UUID，不写真实凭证。
- `tests/conftest.py`：在导入 `src.config` 前建立无秘密 test settings。
- `tests/unit/test_decision_nodes.py`、`tests/unit/test_nodes.py`：清理 Ruff，并更新 `gateway_skill_context` 断言。
- `docs/llm-v2-remediation.md`：仅修改经确认的新 v2 路径和最终验收证据索引。
- `.codex/skills/myagent2-sgai-http-e2e/...`：真实闭环通过后再增加 v2 模式，不提前包装未验证流程。

---

## Chunk 1: 契约、配置与测试基线

### Task 0: 冻结双方 v2 协议基线

**Files:**

- Create: `tests/fixtures/llm_gateway_v2/capabilities.json`
- Create: `tests/fixtures/llm_gateway_v2/session_started.json`
- Create: `tests/fixtures/llm_gateway_v2/observation_updated.json`
- Create: `tests/fixtures/llm_gateway_v2/skill_started.json`
- Create: `tests/fixtures/llm_gateway_v2/skill_finished_with_lease.json`
- Create: `tests/fixtures/llm_gateway_v2/skill_finished_without_lease.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_rejected.json`
- Create: `tests/fixtures/llm_gateway_v2/session_stopped.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_call_skill.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_wait.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_no_op.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_stop_hosting.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_accepted.json`
- Create: `tests/fixtures/llm_gateway_v2/decision_rejected_response.json`
- Create: `tests/fixtures/llm_gateway_v2/gateway_contract.schema.json`
- Create: `tests/fixtures/llm_gateway_v2/gateway_contract_source.json`
- Create: `tests/fixtures/llm_gateway_v2/hmac_vectors.json`
- Create: `tests/fixtures/llm_gateway_v2/gateway_runtime_config_keys.json`
- Create: `tests/unit/llm_gateway_v2/test_contract_fixtures.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify after Gateway approval: `docs/llm-v2-remediation.md`

- [ ] **Step 1: 从 Gateway v2 导出实际 request/response fixture**

fixture 必须来自 Gateway 当前实现导出的 JSON Schema，不能由 myAgent2 单方面猜测 lease、terminal 或 response 字段。`gateway_contract.schema.json` 必须完整表达 envelope、六类 event、ACK、capabilities、四类 decision、accepted/rejected response 的 required/null/extra/oneOf/discriminator/enum 约束，正例 fixture 全部通过该 schema；每类事件/action 另由 Task 3 生成 missing/null/wrong type/extra 负例。

`gateway_contract_source.json` 记录 Gateway repository 标识、commit SHA、schema export command、contractVersion、导出 UTC 时间以及 schema SHA-256；不得记录开发机绝对路径。schema 或来源 commit 改变时，contract test 必须失败并要求双方重新确认，不能静默更新 hash。

`hmac_vectors.json` 使用明确标记为 fixture-only 的假 AppId/secret，固定 method、path、raw body 的 UTF-8/base64、timestampMs、requestId、body SHA-256、canonical signing text 和 Gateway 测试套件导出的 expected signature。至少包含 events 与 decision 两个方向，预期值不能调用 myAgent2 helper 在测试运行时生成。

- [ ] **Step 2: 双方确认路径、字段和认证边界**

确认记录必须明确：capabilities 不使用 HMAC；events/decision 使用 HMAC；外部字段只接受 camelCase；`extra=forbid`；null 与字段缺失的区别；六类事件的 lease 字段组合；四类 decision action 的字段组合。另由 Gateway 维护方提供 Real 模式实际配置键，`gateway_runtime_config_keys.json` 固定包含 `enabledKey/providerBaseUrlKey/contractVersionKey/capabilitiesPathKey/eventsPathKey/eventAppIdKey/eventAppSecretKey/gatewayIdKey/decisionAppIdKey/decisionAppSecretKey`，值只记录 Gateway 实际配置键名；另含双方确认的公开 `decisionPath`（当前 SGAI 为 `/api/v1/hosting/llm/decision`）。fixture 不记录配置值或凭证；Task 17 的 Real 启动脚本必须读取此 fixture，不再内置猜测的 Gateway 配置名或 decision path。

- [ ] **Step 3: 更新 remediation 的正式路径基线**

只有 Gateway 维护方确认后，才把 `receiveEventsPath` 改为 `/api/gateway/v2/events`。未确认时暂停 Task 1 之后的生产实现。

- [ ] **Step 4: 锁定 validator 并校验 fixture**

在 `pyproject.toml` 将 `jsonschema` 声明为直接 dev/test dependency 并更新 `uv.lock`，不能依赖其它包偶然带入的传递依赖。`test_contract_fixtures.py` 先根据 schema 的 `$schema` 使用 `Draft202012Validator.check_schema()` 验证导出 schema 本身，再验证全部正例。

安全检查使用 JSON parser 只扫描叶子值，不扫描 `eventAppSecretKey` 等合法键名；拒绝私网 IP、本机绝对路径、项目当前 `.env` 中的值和未标记为 fixture-only 的 secret。测试同时校验 source manifest 的 commit/SHA-256和固定 HMAC vector 字段完整性。

Run: `uv add --dev "jsonschema>=4.26.0"`

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contract_fixtures.py -q`

Expected: PASS；输出不包含任何 secret 值。

### Task 1: 定义 capabilities model，不提前开放路由

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_capabilities.py`
- Create: `src/core/integration/llm_gateway_v2/__init__.py`
- Create: `src/core/integration/llm_gateway_v2/contracts.py`

- [ ] **Step 1: 写 capabilities RED 测试**

测试必须断言：

```python
capabilities = build_gateway_v2_capabilities(
    max_event_batch_size=100,
    max_decision_ttl_ms=30000,
)
assert capabilities.model_dump(by_alias=True) == {
    "contractVersion": "llm-gateway-http-v2",
    "receiveEventsPath": "/api/gateway/v2/events",
    "supportedDecisionActions": ["call_skill", "wait", "no_op", "stop_hosting"],
    "perEventAck": True,
    "controlGeneration": True,
    "eventSequence": True,
    "asyncSkillTerminal": True,
    "supportedEventTypes": [
        "session_started",
        "observation_updated",
        "skill_started",
        "skill_finished",
        "decision_rejected",
        "session_stopped",
    ],
    "maxEventBatchSize": 100,
    "maxDecisionTtlMs": 30000,
}
```

测试同时验证所有正数限制、固定事件/action 集合、camelCase 序列化和拒绝额外字段。HTTP/OpenAPI 断言推迟到 Task 6 接入真实 durable service 后执行。

- [ ] **Step 2: 运行 RED 测试**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_capabilities.py -q`

Expected: FAIL，原因是 `GatewayV2Capabilities` 尚不存在。

- [ ] **Step 3: 实现 capabilities response model**

model 只表达协议内容，不读取 readiness、不注册路由。max limits 由 settings 注入的 factory 构造，禁止在 model 和 route 中各维护一份常量。

- [ ] **Step 4: 运行 GREEN 测试及 v1 路由回归**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_capabilities.py -q`

Run: `uv run pytest tests/api/test_gateway_v1_events.py -q`

Run: `uv run ruff check src/core/integration/llm_gateway_v2/contracts.py tests/unit/llm_gateway_v2/test_capabilities.py`

Expected: 全部 PASS，v1 路由未受 contracts package 引入影响。

### Task 2: 建立最早边界的无秘密测试配置

**Files:**

- Modify: `src/config.py`
- Modify: `tests/conftest.py`
- Modify: `.env.example`
- Create: `tests/unit/test_config.py`

- [ ] **Step 1: 写无 `.env` RED 测试**

通过 subprocess 在临时 cwd 导入 `src.config`，显式设置 `PYTHONPATH` 为仓库根目录、清除继承的 myAgent 配置变量、只传入测试占位环境变量，并断言 exit code 为 0 且不读取项目 `.env`。另写生产模式缺少 `OPENAI_API_KEY/POSTGRES_DSN/NEO4J_PASSWORD` 时 exit code 非 0 的测试。

- [ ] **Step 2: 验证测试因 settings 仍读取 `.env` 而失败**

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: FAIL，证明 fixture patch 发生得太晚。

- [ ] **Step 3: 实现 test settings 边界**

- `tests/conftest.py` 顶部在任何 `src.*` import 前设置 `ENV=test` 和全部类型合法的占位变量。
- `src/config.py` 增加 `load_settings()`；它先从进程环境读取 `ENV`，test 时调用 `Settings(_env_file=None)`，其它环境调用 `Settings(_env_file=".env")`。全局 `settings` 只由该 factory 创建。
- 占位 URL 使用保留域名，不指向真实服务。
- v2 默认关闭；v1 默认开启。
- 在 Task 2 定义 `LLM_GATEWAY_APP_GATEWAYS: dict[str, list[str]]`；Task 4 只实现认证，不再新增配置字段。
- 新配置及默认值：`LLM_GATEWAY_V1_ENABLED=true`、`LLM_GATEWAY_V2_ENABLED=false`、`EMBEDDING_ENABLED=true`、`LLM_GATEWAY_V2_MAX_EVENT_BATCH_SIZE=100`、`LLM_GATEWAY_V2_MAX_DECISION_TTL_MS=30000`、`LLM_GATEWAY_V2_EVENT_MAX_ATTEMPTS=5`、`LLM_GATEWAY_V2_DECISION_MAX_ATTEMPTS=5`、`LLM_GATEWAY_V2_RETRY_BASE_MS=1000`、`LLM_GATEWAY_V2_RETRY_MAX_MS=300000`、`LLM_GATEWAY_V2_CLAIM_TTL_MS=30000`、`LLM_GATEWAY_V2_POLL_MS=250`、`LLM_GATEWAY_V2_SHUTDOWN_GRACE_SECONDS=10`、`LLM_GATEWAY_V2_READINESS_TIMEOUT_SECONDS=3`、`LLM_GATEWAY_V2_READINESS_CACHE_SECONDS=5`。
- `.env.example` 中 tenant 使用 `00000000-0000-0000-0000-000000000001`，secret 只使用 `<...>` 占位符。

Run: `uv run pytest tests/unit/test_config.py -k "does_not_read_dotenv or production_missing_required" -q`

Expected: PASS，配置约束测试此时尚未实现。

- [ ] **Step 4: 写并运行 v2 配置约束 RED 测试**

逐个覆盖空 v2 allowlist、allowlist AppId 缺 secret、缺 tenant、非法 UUID、缺 outbound 凭证、decision AppId 与 v2 inbound AppId 重用、不同方向使用相同 secret、无效 batch/TTL/retry/claim limits、v2 开启时额外 v1-only `APP_SECRETS` 可共存，以及 v2 disabled 时仅 v1 配置可正常加载。

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: FAIL，原因是对应 validator 尚未实现。

- [ ] **Step 5: 实现 v2 配置 validator**

v2 AppId 集合严格定义为 `LLM_GATEWAY_APP_GATEWAYS.keys()`，不是 `APP_SECRETS.keys()`。v2 启用时要求该集合非空，校验所有 limits 为正且 base retry <= max retry、每个 v2 AppId 有 secret 和非空 gateway allowlist、每个 allowlist gateway 都有 UUID tenant、decision AppId 不属于 v2 AppId 集合、decision secret 不与任何 v2 inbound secret 相同且非空。`APP_SECRETS` 中没有 `APP_GATEWAYS` 条目的额外身份视为 v1-only，允许共存且不得获得 v2 route 权限。

- [ ] **Step 6: 验证配置测试和 Ruff**

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: PASS。

Run: `uv run pytest tests/api/test_gateway_v1_events.py tests/unit/test_robotgateway_callback.py -q`

Expected: PASS，settings 初始化和 v1-only identity 未回归。

Run: `uv run ruff check src/config.py tests/conftest.py tests/unit/test_config.py`

Expected: 0 errors。

### Task 3A: 定义 batch envelope、ACK 和错误 schema

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_contracts.py`
- Modify: `src/core/integration/llm_gateway_v2/contracts.py`

- [ ] **Step 1: 写 envelope/ACK/error RED 测试**

公共 envelope 固定为 `traceId/gatewayId/contractVersion/sentAtMs/events`，`contractVersion` 只能是 `llm-gateway-http-v2`；逐字段测试 missing/wrong type/null/extra/snake_case。ACK 只能包含 `accepted/traceId/receivedEventIds/duplicateEventIds`；错误 response 只能包含稳定 `error.code/error.message`。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -k "envelope or ack or protocol_error" -q`

Expected: FAIL，models 不存在。

- [ ] **Step 3: 实现 envelope/ACK/error models**

严格按 Task 0 schema 实现 aliases、required/null/extra 和固定 contractVersion；不加入 schema 未声明的 partial ACK 字段。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -k "envelope or ack or protocol_error" -q`

Expected: PASS。

### Task 3B: 定义六类 event discriminated union

**Files:**

- Modify: `tests/unit/llm_gateway_v2/test_contracts.py`
- Modify: `src/core/integration/llm_gateway_v2/contracts.py`

- [ ] **Step 1: 为每类事件分别写合法和非法 RED 测试**

从 Task 0 的冻结 fixture 加载 JSON。公共 event 字段为 `eventId/eventType/sessionId/controlGeneration/eventSequence/occurredAtMs`，每个根字段都覆盖 missing/wrong type/null/extra；外部 snake_case 必须拒绝。

约束：

- `session_started`：sequence 必须为 1，必须有 lease/state/session/skills/hints。
- `observation_updated`：必须有 lease/state/session/skills/hints。
- `skill_started`：必须有 decisionId/skillCallId，不允许 lease。
- `skill_finished`：必须有 decisionId/skillCallId/terminal；lease context 整体可选，不允许半套字段。
- `decision_rejected`：必须有 decisionId 和非空 reason，不允许 lease。
- `session_stopped`：必须有 stop reason，不允许 lease。

- [ ] **Step 2: 运行 event RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -k "session_started or observation_updated or skill_started or skill_finished or decision_rejected or session_stopped" -q`

Expected: FAIL，event models 尚不存在。

- [ ] **Step 3: 实现独立 payload models**

每个 model 使用 `ConfigDict(extra="forbid", populate_by_name=False)` 和显式 camelCase alias。使用 alias 已验证的 discriminated union，不允许一个全字段可选的大 model。

- [ ] **Step 4: 运行 event GREEN**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -k "session_started or observation_updated or skill_started or skill_finished or decision_rejected or session_stopped" -q`

Expected: PASS。

### Task 3C: 定义 terminal schema

**Files:**

- Modify: `tests/unit/llm_gateway_v2/test_contracts.py`
- Modify: `src/core/integration/llm_gateway_v2/contracts.py`

- [ ] **Step 1: 写 terminal RED 测试**

terminal 规则以 Task 0 冻结 schema 为准，不额外要求 success 携带 remediation 未规定的 reason/retryable：

- `success` 不允许 `failureCategory`。
- `failed` 只允许四类 `failureCategory`。
- `cancelled/timeout` 不允许 `failureCategory`。
- 当对应状态允许/要求 `reason` 时必须是稳定非空字符串；当存在 `retryable` 时必须为 bool。

- [ ] **Step 2: 运行 terminal RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -k terminal -q`

Expected: FAIL，terminal union 尚不存在。

- [ ] **Step 3: 实现 terminal models**

严格照 frozen JSON Schema 建立 discriminated union 和交叉字段 validator，不从示例值外推规则。

- [ ] **Step 4: 运行 terminal GREEN**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -k terminal -q`

Expected: PASS。

### Task 3D: 定义四类 decision request 和通用 response schema

**Files:**

- Modify: `tests/unit/llm_gateway_v2/test_contracts.py`
- Modify: `src/core/integration/llm_gateway_v2/contracts.py`

- [ ] **Step 1: 写 decision RED 测试**

- decision request 的四种 action 使用独立 discriminated model。
- 对 `contractVersion/decisionId/decisionLeaseId/stateVersion/controlGeneration/ttlMs/action` 逐字段测试 missing/wrong type/null/extra。
- `call_skill` 必须有 skillName/schemaVersion/arguments；`wait` 只允许 waitMs；`no_op` 不允许 skill 字段；`stop_hosting` 不允许 arguments。
- rejected response 的 reason 只要求非空字符串，不使用本地 allowlist。

通用 response model 只解析 accepted/rejected 字段组合。依赖原始 request action 的 skillCallId 条件由 Task 12 的 `validate_decision_response(request_action, http_status, response_model)` 完成，并在那里做 RED/GREEN 测试。

- [ ] **Step 2: 运行 decision RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -q`

Expected: FAIL，decision models 尚不存在或字段组合尚未满足。

- [ ] **Step 3: 实现 decision request/response models**

严格照 frozen JSON Schema 实现四类 request 和通用 response；不在通用 response model 中提前加入依赖 request action 的验证。

- [ ] **Step 4: 运行 GREEN 和 Ruff**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_contracts.py -q`

Expected: PASS。

Run: `uv run ruff check src/core/integration/llm_gateway_v2/contracts.py tests/unit/llm_gateway_v2/test_contracts.py`

Expected: 全部通过。

### Task 4: 分离双向身份并绑定 AppId/gatewayId/tenant

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_auth.py`
- Create: `src/core/integration/llm_gateway_v2/auth.py`

- [ ] **Step 1: 写身份 RED 测试**

覆盖：正确入站身份、缺失 header、非法 requestId/signature、method/path/body 篡改、时间窗边界、过期时间戳、未知 AppId、AppId 声明未授权 gatewayId、缺 tenant、非 UUID tenant、把出站 AppId 当入站 AppId、v1-only AppId 调用 v2，以及稳定 401/400 error mapping。时钟通过参数注入。

固定向量测试直接读取 Task 0 的 `hmac_vectors.json`：入站 verifier 必须验证 Gateway 导出的 events signature，出站 builder 必须精确产生 Gateway 导出的 decision headers；另比较现有 v1 HMAC helper 的共享算法结果。测试不得先用被测 builder 生成签名再喂给 verifier。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_auth.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现 auth API**

```python
@dataclass(frozen=True)
class GatewayAuthError(Exception):
    code: Literal[
        "auth_header_invalid",
        "auth_timestamp_invalid",
        "signature_invalid",
        "app_id_unknown",
        "gateway_not_authorized",
        "tenant_not_configured",
    ]
    http_status: Literal[400, 401]

@dataclass(frozen=True)
class InboundGatewayIdentity:
    app_id: str
    gateway_id: str
    tenant_id: UUID

def verify_inbound_hmac(
    method: str,
    path: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    now_ms: int,
) -> str: ...  # returns authenticated app_id

def resolve_inbound_identity(app_id: str, gateway_id: str) -> InboundGatewayIdentity: ...

def build_outbound_hmac_headers(
    method: str,
    path: str,
    raw_body: bytes,
    app_id: str,
    app_secret: SecretStr,
    request_id: str,
    timestamp_ms: int,
) -> dict[str, str]: ...
```

使用 Task 2 已定义的 `LLM_GATEWAY_APP_GATEWAYS`。v2 身份关系为：只有 `APP_GATEWAYS` 中的 AppId 才是 v2 AppId，其 secret 来自 `APP_SECRETS`，gatewayId 的 UUID tenant 来自 `APP_TENANTS`；允许 `APP_SECRETS` 保留不在 `APP_GATEWAYS` 中的 v1 身份，但它们调用 v2 必须得到 `app_id_unknown`。

所有认证失败只抛 `GatewayAuthError`，异常 message 为空且不保存原始 header/signing text。Task 6 route 是唯一 HTTP 映射点：捕获该 typed exception 后使用 `http_status` 和冻结 `GatewayV2Error` 输出 code；Pydantic/schema 失败统一 400 `bad_request`；其它异常不得误映射成认证错误。响应和日志不返回 secret、tenant 配置原文或 signing text。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_auth.py -q`

Expected: PASS。

Run: `uv run ruff check src/core/integration/llm_gateway_v2/auth.py tests/unit/llm_gateway_v2/test_auth.py`

Expected: 0 errors。

---

## Chunk 2: PostgreSQL durable inbox 与顺序状态机

### Task 5: 创建 session fence、control cycle 和 inbox migration

**Files:**

- Create: `alembic/versions/008_llm_gateway_v2_inbox.py`
- Create: `tests/integration/test_gateway_v2_migrations.py`
- Create: `tests/integration/conftest.py`
- Modify: `alembic/env.py`

- [ ] **Step 1: 写 migration RED 测试**

在 `tests/integration/conftest.py` 建立对整个 integration test tree 生效的 session fixture：只接受显式 `TEST_POSTGRES_DSN`，用 SQLAlchemy `make_url()` 解析并拒绝数据库名不以 `myagent_test_` 开头的 DSN。fixture 创建独立 Alembic `Config`，通过 `config.attributes["database_url_override"]` 传入测试 URL，禁止读取开发 `.env`。测试先断言 Alembic 实际 connection 的 `current_database()` 仍以 `myagent_test_` 开头，再在该一次性数据库执行 `007 -> 008 -> 007 -> 008`，逐项断言表、列、server default、nullable、FK、CHECK、unique、partial index 和对称 downgrade。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/integration/test_gateway_v2_migrations.py -q`

Expected: FAIL，缺少 revision 008。

- [ ] **Step 3: 让显式 migration URL 优先于应用 settings**

修改 `alembic/env.py`：若 `config.attributes.get("database_url_override")` 存在，则将该值写入 `sqlalchemy.url`；否则才读取 `settings.postgres_dsn`。写入 ConfigParser 前转义 URL 中的 `%`，不得使用 `alembic.ini` 的 placeholder，也不得用测试 URL覆盖进程级 `POSTGRES_DSN`。增加测试证明 override 优先于一个故意指向其它数据库的 `settings.postgres_dsn`，并通过 `SELECT current_database()` 验证最终连接目标。

- [ ] **Step 4: 实现 `llm_gateway_sessions`**

```text
id UUID PK DEFAULT gen_random_uuid()
tenant_id UUID NOT NULL FK tenants.id ON DELETE CASCADE
gateway_id VARCHAR(128) NOT NULL
session_id VARCHAR(128) NOT NULL
current_generation BIGINT NULL CHECK > 0
fence_version BIGINT NOT NULL DEFAULT 0 CHECK >= 0
status VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK pending/active/stopped/manual
created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE(gateway_id, session_id) name=uq_llm_gateway_sessions_identity
INDEX ix_llm_gateway_sessions_tenant_status(tenant_id, status)
```

- [ ] **Step 5: 实现 `llm_gateway_control_cycles`**

关键列：

```text
id UUID PK
tenant_id UUID NOT NULL FK tenants.id ON DELETE CASCADE
runtime_session_id UUID NOT NULL FK llm_gateway_sessions.id ON DELETE CASCADE
gateway_id VARCHAR(128) NOT NULL
session_id VARCHAR(128) NOT NULL
control_generation BIGINT NOT NULL CHECK > 0
status VARCHAR(24) NOT NULL CHECK pending/active/stopped/superseded/manual
next_event_sequence BIGINT NOT NULL DEFAULT 1 CHECK > 0
latest_state_version BIGINT NULL CHECK >= 0
latest_decision_lease_id VARCHAR(128) NULL
latest_decision_context JSONB NULL
created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
started_at/stopped_at TIMESTAMPTZ NULL
UNIQUE(gateway_id, session_id, control_generation) name=uq_llm_gateway_cycles_partition
INDEX ix_llm_gateway_cycles_runnable(status, next_event_sequence, updated_at)
```

- [ ] **Step 6: 实现 `llm_gateway_events`**

关键列：

```text
id UUID PK
tenant_id UUID NOT NULL FK tenants.id ON DELETE CASCADE
cycle_id UUID NOT NULL FK llm_gateway_control_cycles.id ON DELETE CASCADE
gateway_id VARCHAR(128) NOT NULL
session_id VARCHAR(128) NOT NULL
event_id VARCHAR(128) NOT NULL
event_type VARCHAR(32) NOT NULL CHECK six v2 event types
control_generation BIGINT NOT NULL
event_sequence BIGINT NOT NULL CHECK > 0
content_hash CHAR(64) NOT NULL
event_body JSONB NOT NULL
trace_id VARCHAR(128) NOT NULL -- 单独存储，不参与 hash
status VARCHAR(32) NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
claim_token UUID NULL
claimed_fence_version BIGINT NULL
lock_until TIMESTAMPTZ NULL
locked_by VARCHAR(128) NULL
error_stage/error_category VARCHAR(64) NULL
received_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
started_at/completed_at TIMESTAMPTZ NULL
UNIQUE(gateway_id, event_id) name=uq_llm_gateway_events_identity
UNIQUE(gateway_id, session_id, control_generation, event_sequence) name=uq_llm_gateway_events_partition_sequence
CHECK((event_type='session_started' AND event_sequence=1) OR (event_type<>'session_started' AND event_sequence>1))
INDEX ix_llm_gateway_events_due(status, next_attempt_at, received_at)
UNIQUE PARTIAL INDEX uq_llm_gateway_events_cycle_processing(cycle_id) WHERE status='processing'
```

status CHECK：`pending/processing/succeeded/retryable_failed/dead_letter/manual/superseded`。

- [ ] **Step 7: 仅在隔离测试库验证 upgrade/downgrade**

Run: `if (-not $env:TEST_POSTGRES_DSN) { throw 'TEST_POSTGRES_DSN must target a myagent_test_* database' }; uv run pytest tests/integration/test_gateway_v2_migrations.py -q`

Expected: PASS。不得直接对当前 `POSTGRES_DSN` 执行 downgrade。

### Task 6: 实现 canonical event hash 和 durable batch ACK

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_canonical.py`
- Create: `tests/unit/llm_gateway_v2/test_event_service.py`
- Create: `tests/integration/llm_gateway_v2/test_inbox_repository.py`
- Create: `tests/api/test_gateway_v2.py`
- Create: `src/core/integration/llm_gateway_v2/canonical.py`
- Create: `src/core/integration/llm_gateway_v2/inbox_repository.py`
- Create: `src/core/integration/llm_gateway_v2/event_service.py`
- Create: `src/api/routes/gateway_v2.py`
- Modify: `src/api/main.py`

- [ ] **Step 1: 写 hash RED 测试**

同一 event 更换 `traceId/sentAtMs/requestId` 后 hash 必须相同；修改 payload、generation 或 sequence 后 hash 必须不同。

- [ ] **Step 2: 写 ACK RED 测试**

覆盖 new、duplicate、同 ID 不同内容 conflict、混合 new+duplicate、batch 内相同 ID/相同内容、batch 内冲突、单项 savepoint 中 `IntegrityError/DataError` 的 partial ACK、连接失败、deadlock、serialization failure 和 commit 失败的全批 503、tenant 无效不进 repository、返回 ID 去重且保持首次出现顺序、响应发送失败后的整批重投。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_canonical.py tests/unit/llm_gateway_v2/test_event_service.py -q`

Expected: FAIL。

- [ ] **Step 4: 实现 admission transaction**

repository API：

```python
async def accept_event_batch(
    identity: InboundGatewayIdentity,
    trace_id: str,
    events: Sequence[GatewayV2Event],
) -> BatchAcceptance: ...
```

算法：先在内存检测 batch 内重复/冲突；事务内批量锁定/查询已有 event。每项使用 savepoint 和 `INSERT ... ON CONFLICT DO NOTHING RETURNING`；未返回时读取 content hash 区分 duplicate/conflict。任何 content conflict 回滚外层事务并返回 409；commit 后才生成 ACK。为每个 event upsert session/cycle，但 cycle 保持 pending，只有 worker 处理合法 session_started 才激活。

partial ACK 的异常边界必须写死：只有当前 event 单条 statement 产生、连接仍有效且可在 savepoint 内恢复的 SQLAlchemy `IntegrityError`/`DataError`，才回滚该 savepoint；该 event 不出现在 `receivedEventIds` 或 `duplicateEventIds` 中，由 Gateway 根据遗漏项重试，ACK 不增加合同外字段。唯一键错误仍须先重查 hash，区分 duplicate 与 content conflict。连接断开、`DBAPIError.connection_invalidated`、commit/flush 外错误、deadlock SQLSTATE `40P01`、serialization failure `40001`、数据库不可用或无法确认事务结果，必须回滚整批并返回 503，不能混入成功 ACK。测试用真实 PostgreSQL fault injection 固定这条分类边界。

- [ ] **Step 5: 写并发 admission RED 测试**

两个真实 PostgreSQL transaction 同时接纳相同 eventId：相同内容必须稳定得到一个 received 和一个 duplicate；不同内容必须只有一个提交，另一个 409，不能得到 500。

Run: `if (-not $env:TEST_POSTGRES_DSN) { throw 'TEST_POSTGRES_DSN must target a myagent_test_* database' }; uv run pytest tests/integration/llm_gateway_v2/test_inbox_repository.py -q`

Expected: FAIL，直到原子 conflict handling 完成。

- [ ] **Step 6: 接入真实 v2 HTTP route**

route 顺序固定为：读取 raw body -> `verify_inbound_hmac` -> parse envelope -> `resolve_inbound_identity` -> max batch 校验 -> `accept_event_batch`。注册 `APIRouter(prefix="/api/gateway/v2", tags=["gateway-v2"])`；events 使用 HMAC，capabilities route 此时仍返回 503，不虚报 ready。

- [ ] **Step 7: 运行 GREEN 和 API/v1 回归**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_canonical.py tests/unit/llm_gateway_v2/test_event_service.py tests/api/test_gateway_v2.py -q`

Expected: PASS。

Run: `uv run pytest tests/api/test_gateway_v1_events.py -q`

Expected: PASS，v1 路径和行为不变。

### Task 7: 实现 partition claim、gap 和 generation 抑制

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_event_worker.py`
- Create: `tests/integration/test_gateway_v2_recovery.py`
- Create: `src/core/integration/llm_gateway_v2/event_worker.py`
- Create: `src/core/integration/llm_gateway_v2/worker_status.py`
- Modify: `src/core/integration/llm_gateway_v2/inbox_repository.py`

- [ ] **Step 1: 写顺序 RED 测试**

覆盖两个 session 并行、sequence 2 先到时不处理、sequence 1 到达后依次处理、同一 partition 不并发、不同 partition 可并发。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_event_worker.py::test_gap_blocks_later_sequence tests/unit/llm_gateway_v2/test_event_worker.py::test_different_sessions_can_run_concurrently -q`

Expected: FAIL，event worker/claim API 尚不存在。

- [ ] **Step 2: 写 generation RED 测试**

写成参数化矩阵并固定预期：

- `incoming > current`：仅合法 `session_started(sequence=1)` 可以在锁定 runtime 后切换 generation、递增一次 `fenceVersion`、激活新 cycle 并 supersede 所有旧 active/pending cycle；其它事件保持 pending/manual，不得隐式建立新 generation。
- `incoming == current`：重复 `session_started` 按 eventId/hash 幂等；后续事件按 sequence 处理，不递增 fence。
- `incoming < current`：事件标记 superseded，可推进该旧 cycle 的内部 sequence 以便排空，但不能更新 runtime/current context/latest lease、运行 Agent或创建 decision。
- `session_stopped`：只关闭事件自身 generation；旧 generation 的 stop 不能停止当前新 generation。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_event_worker.py::test_generation_transition_matrix -q`

Expected: FAIL，generation fence 尚未实现。

- [ ] **Step 3: 写 crash recovery RED 测试**

将事件置为 processing 且 lock 过期，启动新 worker 后必须用新 claimToken reclaim 同一 event；旧 worker用旧 token/fence 提交必须 CAS 失败；每次首次 claim/reclaim 的 `attempt_count` 恰好递增一次，原始 event body/content hash 不变化。连续在处理器中崩溃并让 lock 过期，最终必须达到 attempt 上限并 dead-letter，不能无限 reclaim。

Run: `uv run pytest tests/integration/test_gateway_v2_recovery.py::test_expired_claim_is_fenced_and_bounded -q`

Expected: FAIL，reclaim/CAS 尚未实现。

- [ ] **Step 4: 实现 claim SQL**

定义可注入接口，使 Chunk 2 不依赖后续业务实现：

```python
class EventProcessor(Protocol):
    async def __call__(self, event: ClaimedGatewayEvent) -> EventProcessResult: ...

@dataclass(frozen=True)
class EventProcessResult:
    outcome: Literal["succeeded", "retryable_failed", "manual"]
    error_stage: str | None = None
    error_category: str | None = None
```

使用 `FOR UPDATE SKIP LOCKED` 锁 session runtime 和 cycle。pending cycle 只有 session_started/sequence 1 可以激活；active cycle 只有 `event_sequence=next_event_sequence` 才能 claim。claim/reclaim 在同一 SQL transaction 中生成 UUID claimToken、复制 session fenceVersion、递增 `attempt_count`、写 lockUntil 后提交；`attempt_count >= EVENT_MAX_ATTEMPTS` 的过期 processing 行由 recovery sweep 原子转为 dead_letter，不再领取。处理完成必须按 `event.id + claimToken + claimedFenceVersion + status=processing` CAS 更新，并重新检查 runtime 的 `current_generation/fence_version`。

- [ ] **Step 5: 实现完成和失败迁移**

- success：event succeeded，cycle next sequence +1，清 lock。
- retryable：使用 PostgreSQL `clock_timestamp()`；claim 阶段已经递增 attempt，此处不再递增，delay=`min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** (attempt_count-1))`，清 claim，不推进 sequence。
- attempt 达 `EVENT_MAX_ATTEMPTS`：dead_letter，分区停在当前 sequence；worker 保持 ready，但 heartbeat snapshot 暴露 degraded/deadLetterCount，日志发稳定告警。
- manual：不自动重试、不推进 sequence，等待人工修复。
- stale generation：superseded 并按规则推进旧分区，但不得修改新 generation 状态。
- 数据库状态更新失败：保留 processing claim，等待 lockUntil 后由新 worker reclaim。

- [ ] **Step 6: 实现 session generation fence 和 heartbeat**

按 Step 2 的 generation 矩阵实现。新 generation 的 session_started 在事务中锁 session runtime，递增 fenceVersion、设置 currentGeneration、supersede 旧 cycle；同 generation 重投不递增。每次 event claim、Agent 后 plan commit 和完成都校验 current generation/fence。`WorkerStatusRegistry` 每个 poll loop 更新 monotonic heartbeat，记录 running/draining/stopped 和最近成功 poll 时间。

- [ ] **Step 7: 增加双 worker/CAS/失败上限测试并运行 GREEN**

测试覆盖双 worker 竞争、gap、claim reclaim、旧 worker 迟到完成、retry 上限、dead-letter 阻塞、manual 阻塞、CAS 失败和不同 session 并发。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_event_worker.py tests/integration/test_gateway_v2_recovery.py -q`

Expected: PASS。

---

## Chunk 3: Agent、decision outbox 与唯一终态

### Task 8: 创建 decision outbox 和 skill call migration

**Files:**

- Create: `alembic/versions/009_llm_gateway_v2_outbox.py`
- Modify: `tests/integration/test_gateway_v2_migrations.py`

- [ ] **Step 1: 扩展 migration RED 测试**

在隔离测试库断言 decisionId/body、source event、decision lease、skillCallId、terminal/effect、claim token 和 action tracking FK；先运行并确认因 revision 009 不存在而失败。

Run: `if (-not $env:TEST_POSTGRES_DSN) { throw 'TEST_POSTGRES_DSN must target a myagent_test_* database' }; uv run pytest tests/integration/test_gateway_v2_migrations.py -q`

Expected: FAIL，缺少 revision 009。

- [ ] **Step 2: 实现 `llm_gateway_decisions`**

必须包含：

```text
id UUID PK DEFAULT gen_random_uuid()
tenant_id UUID NOT NULL FK tenants.id ON DELETE CASCADE
cycle_id UUID NOT NULL FK llm_gateway_control_cycles.id ON DELETE CASCADE
source_event_id UUID NOT NULL FK llm_gateway_events.id ON DELETE RESTRICT
action_tracking_id UUID NULL FK action_tracking.id ON DELETE SET NULL
gateway_id/session_id/decision_id/decision_lease_id VARCHAR(128) NOT NULL
control_generation/state_version BIGINT NOT NULL
lease_expires_at_ms BIGINT NULL
action VARCHAR(24) NOT NULL CHECK call_skill/wait/no_op/stop_hosting
request_body_json JSONB NOT NULL
request_body_bytes BYTEA NOT NULL
body_hash CHAR(64) NOT NULL
status VARCHAR(32) NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
claim_token UUID/claimed_fence_version BIGINT/locked_by VARCHAR/lock_until TIMESTAMPTZ NULL
response_http_status INTEGER/response_status VARCHAR/response_reason VARCHAR/skill_call_id VARCHAR NULL
error_stage/error_category VARCHAR(64) NULL
created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
sent_at/completed_at TIMESTAMPTZ NULL
UNIQUE(gateway_id, decision_id)
UNIQUE(source_event_id)
UNIQUE(gateway_id, decision_lease_id)
INDEX ix_llm_gateway_decisions_due(status, next_attempt_at, created_at)
```

status CHECK：`planned/sending/accepted/rejected/retryable_failed/dead_letter/cancelled/manual`。

- [ ] **Step 3: 实现 `llm_gateway_skill_calls`**

完整列定义：

```text
id UUID PK DEFAULT gen_random_uuid()
tenant_id UUID NOT NULL FK tenants.id ON DELETE CASCADE
decision_row_id UUID NOT NULL FK llm_gateway_decisions.id ON DELETE RESTRICT
terminal_event_id UUID NULL FK llm_gateway_events.id ON DELETE RESTRICT
gateway_id/session_id/decision_id/skill_call_id VARCHAR(128) NOT NULL
skill_name VARCHAR(128) NOT NULL
status VARCHAR(24) NOT NULL DEFAULT 'pending'
failure_category VARCHAR(32) NULL
reason VARCHAR(256) NULL
retryable BOOLEAN NULL
effect_status VARCHAR(24) NOT NULL DEFAULT 'not_applicable'
effect_applied_at TIMESTAMPTZ NULL
created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
started_at/completed_at TIMESTAMPTZ NULL
CHECK status IN (pending,started,succeeded,failed,cancelled,timeout,manual)
CHECK failure_category IS NULL OR failure_category IN (business_rejected,transport_failed,protocol_failed,internal_failed)
CHECK effect_status IN (not_applicable,pending,applied,manual)
UNIQUE(gateway_id, skill_call_id) name=uq_llm_gateway_skill_calls_identity
UNIQUE(terminal_event_id) name=uq_llm_gateway_skill_calls_terminal_event
INDEX ix_llm_gateway_skill_calls_decision(decision_row_id)
INDEX ix_llm_gateway_skill_calls_status(tenant_id, status, updated_at)
```

`call_skill` accepted 使用实际 skillName；`stop_hosting` accepted 使用稳定内部值 `stop_hosting`，同样建立 pending call。wait/no_op 不创建 call。

- [ ] **Step 4: 验证 migration**

Run: `uv run pytest tests/integration/test_gateway_v2_migrations.py -q`

Expected: PASS。

### Task 9: 建立 v2 无副作用决策图和 Gateway 上下文

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_decision_service.py`
- Create: `src/core/agents/gateway_v2.py`
- Create: `src/core/agents/gateway_v2_models.py`
- Create: `src/core/agents/gateway_v2_prompts.py`
- Create: `src/core/integration/llm_gateway_v2/decision_service.py`

- [ ] **Step 1: 写 Agent 能力收窄 RED 测试**

覆盖 availableSkills 子集、schemaVersion、allowed args、missing args、lease action scope、movement_control、禁止 ground、fallback action 权限。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -k "context or selector" -q`

Expected: FAIL，v2 models/selector 尚不存在。

- [ ] **Step 2: 实现独立 v2 context/model/prompt 并运行 GREEN**

v2 context 至少包含安全 session snapshot、availableSkills、skillArgumentHints、lease kind、allowed decision actions、terminal result、generation、sequence、stateVersion。v2 action model 使用动态 skillName 字符串和独立 validator；不能修改共享 v1 `RecommendedAction` 的外部合同。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -k "context or selector" -q`

Expected: PASS。

- [ ] **Step 3: 写 `play_action.actionId` RED 测试**

v2 model、prompt、序列化必须只接受 `actionId`，明确拒绝旧 `action`；v1 model 继续接受原有 `action`。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -k play_action -q`

Expected: FAIL。

- [ ] **Step 4: 实现 v2 actionId 并验证 v1 回归**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -k play_action -q`

Run: `uv run pytest tests/unit/test_models.py tests/unit/test_robotgateway_callback.py tests/api/test_gateway_v1_events.py -q`

Expected: 全部 PASS，证明 v1 `action` 未被破坏。

- [ ] **Step 5: 实现无副作用决策图和最终 selector**

复用读取、RAG、意图、目标和行为分析节点，但 v2 使用独立 action reasoning node，不运行现有 `tracking_update/memory_update`。LLM 输出不直接发送；selector 再次检查 skill/schema/argument paths/lease scope。无合法 skill 时，只能从 lease 允许的 `wait/no_op/stop_hosting` 中选择。

- [ ] **Step 6: 检查 Agent errors**

v2 wrapper 发现任何模型、RAG、数据库节点错误时抛出结构化 retryable exception，不得把空 actions 转成成功 wait。

- [ ] **Step 7: 分别运行 Agent failure RED/GREEN 和 Ruff**

先让 `test_agent_error_becomes_retryable_failure` 因 wrapper 未提升错误而失败，再实现提升并运行：

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -q`

Expected: PASS。

Run: `uv run ruff check src/core/agents/gateway_v2.py src/core/agents/gateway_v2_models.py src/core/agents/gateway_v2_prompts.py src/core/integration/llm_gateway_v2/decision_service.py tests/unit/llm_gateway_v2/test_decision_service.py`

Expected: 0 errors。

### Task 10: 按六类事件分派业务语义

**Files:**

- Modify: `src/core/integration/llm_gateway_v2/event_service.py`
- Modify: `src/core/integration/llm_gateway_v2/event_worker.py`
- Modify: `src/core/integration/llm_gateway_v2/inbox_repository.py`
- Create: `src/core/integration/llm_gateway_v2/outbox_repository.py`
- Create: `src/core/integration/llm_gateway_v2/terminal_repository.py`
- Create: `src/core/integration/llm_gateway_v2/terminal_effect_service.py`
- Create: `tests/unit/llm_gateway_v2/test_terminal_state.py`

- [ ] **Step 1: 写分派 RED 测试**

- session_started/observation_updated：允许 Agent 和 decision。
- skill_started：只 upsert started call。
- skill_finished 无 lease：只收敛 terminal。
- skill_finished 有 lease：先收敛 terminal，再允许 Agent。
- decision_rejected：只合并 decision rejected。
- session_stopped：关闭 generation，取消该 generation 中 `planned/sending/retryable_failed` decision，并把该 cycle 仍为 pending/started 的普通 skill call 收敛为 cancelled、绑定稳定脱敏 stop reason；迟到 worker 因 fence/CAS 不能回写 accepted。`stop_hosting_requested` 的特殊成功规则见 Step 6。

所有 lease-bearing event 必须额外覆盖：只在 `event.id + claimToken + claimedFenceVersion + runtime.currentGeneration/fenceVersion` 仍匹配时，原子更新 cycle 的 `latest_decision_lease_id/latest_state_version` 和安全 session context。旧 worker或旧 generation 不能覆盖较新的 lease/context。

- [ ] **Step 2: 写唯一终态 RED 测试**

覆盖 success、四类 failed、cancelled、timeout、duplicate eventId、不同 eventId 的相同终态、冲突终态、completion_unconfirmed 禁止自动重试。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_terminal_state.py -q`

Expected: FAIL。

- [ ] **Step 4: 实现 conditional terminal update**

使用 SQL 条件更新/行锁保证只有 pending/started 可以进入终态。相同终态幂等返回；不同终态进入 manual 并记录脱敏冲突类别。

- [ ] **Step 5: 写 terminal effect RED 测试并实现最多一次业务推进**

Task 11 的 plan transaction 已为带完整 goal metadata 的可追踪 call_skill 创建 `action_tracking` 行并保存 FK；没有 FK 的 call 固定 `effect_status=not_applicable`。首个 terminal 在同一事务中将有 FK 的 call 置为 `effect_status=pending`，然后按 success/failed/cancelled/timeout 将对应 action tracking 更新为 completed/abandoned/abandoned/timeout，并设 applied；任一步失败整笔回滚，重复 terminal 不再次更新。没有确定映射的 player_intent/player_memory 不从 Gateway 自由文本猜测，保持不变。

- [ ] **Step 6: 定义 HTTP rejected/event rejected 合并矩阵**

- pending/sending + rejected -> rejected。
- rejected + 同 reason rejected event -> 幂等。
- rejected + 不同 reason -> 保留首个稳定结果并记录辅助 reason，不改为 accepted。
- accepted + decision_rejected event -> manual conflict，不创建新 lease/decision。
- skill event 先到时按已有 decisionId upsert call；迟到 accepted 只合并相同 skillCallId。

`stop_hosting` accepted 的 pending call 不等待 `skill_finished`。同 cycle 后续 `session_stopped(reason=stop_hosting_requested)` 在关闭 cycle 的同一事务中，将唯一 pending/started stop_hosting call 更新为 succeeded、绑定该 `terminal_event_id`，`effect_status` 保持 not_applicable；其它 stop reason 不得伪造该 call 成功，转为 cancelled 或 manual 的规则以 Task 0 冻结 fixture 为准。

- [ ] **Step 7: 运行 GREEN**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_terminal_state.py tests/unit/llm_gateway_v2/test_event_worker.py -q`

Expected: PASS。

### Task 11: 生成稳定 decision 并写入 outbox

**Files:**

- Modify: `src/core/integration/llm_gateway_v2/decision_service.py`
- Modify: `src/core/integration/llm_gateway_v2/outbox_repository.py`
- Modify: `src/core/integration/llm_gateway_v2/contracts.py`
- Modify: `tests/unit/llm_gateway_v2/test_decision_service.py`

- [ ] **Step 1: 写稳定性 RED 测试**

同一 source event 重试必须读取同一 persisted decisionId、JSON、原始 UTF-8 bytes 和 body hash；两个 source event 使用同一 lease 时数据库只允许一个 decision；不同 body 试图复用 decisionId 时进入 manual，不发送 HTTP。四类 action 都要做字段组合与序列化测试。

- [ ] **Step 2: 实现 plan transaction**

Agent 成功后开启 plan transaction，锁定 source event、cycle 和 runtime，并要求 source event 仍满足 `status=processing + claimToken + claimedFenceVersion`，cycle 仍 active、`latest_decision_lease_id/latest_state_version` 等于本次 Agent 输入，runtime 的 `current_generation/fence_version` 仍等于 claim；任一条件不满足则 CAS 失败并丢弃 Agent 输出，不写 outbox。

条件成立后，在同一事务内生成 decisionId、构造完整 v2 body、只 canonical serialize 一次并同时保存 JSONB、原始 UTF-8 bytes 和 SHA-256，再插入 planned outbox。body 必须携带原始 `controlGeneration`。后续发送只能读取 `request_body_bytes`，不能从 JSONB 重新序列化。

若 v2 action model 为 `call_skill` 且包含完整、selector 已校验的 goal metadata（`user_id/action_type/goal_metric/goal_value/baseline_value/expected_hours`），同一事务创建 `action_tracking(status=tracking)` 并写入 decision 的 `action_tracking_id`；缺少任一必要 metadata、wait/no_op/stop_hosting 都不创建 tracking，后续 skill call 的 `effect_status=not_applicable`。tracking insert 或 outbox insert 任一失败都回滚整个 plan transaction。

- [ ] **Step 3: 增加 plan fencing/action tracking RED-GREEN**

覆盖 Agent 运行期间 generation 切换、claim 被回收、lease/stateVersion 被更新、同 lease 双 worker、完整 goal metadata、缺失 goal metadata和 tracking insert 失败；断言只有仍持有 claim/fence 的 worker 能写一条 decision，tracking 与 decision 要么同时存在，要么同时不存在。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -k "stable or fencing or tracking" -q`

Expected: PASS。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_service.py -q`

Expected: PASS。

### Task 12: 实现 body-first decision client 和 outbox worker

**Files:**

- Create: `tests/unit/llm_gateway_v2/test_decision_client.py`
- Create: `tests/unit/llm_gateway_v2/test_decision_worker.py`
- Create: `src/core/integration/llm_gateway_v2/decision_client.py`
- Create: `src/core/integration/llm_gateway_v2/decision_worker.py`
- Modify: `src/core/integration/llm_gateway_v2/outbox_repository.py`
- Modify: `src/core/integration/llm_gateway_v2/terminal_repository.py`
- Modify: `src/core/integration/llm_gateway_v2/contracts.py`
- Modify: `src/core/integration/llm_gateway_v2/worker_status.py`

- [ ] **Step 1: 写 HTTP RED 测试**

覆盖 2xx accepted、非 2xx 合法 rejected、unknown rejected reason、非 JSON、非法 accepted、call_skill/stop_hosting accepted 缺 skillCallId、wait/no_op 错误携带 skillCallId、timeout 后相同 raw bytes 重试、idempotency conflict。

- [ ] **Step 2: 写乱序 RED 测试**

覆盖 accepted 先到和 skill_started/skill_finished 先到，两种顺序最终只能形成一个 call；迟到 accepted 只合并 skillCallId。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_client.py tests/unit/llm_gateway_v2/test_decision_worker.py -q`

Expected: FAIL。

- [ ] **Step 4: 实现 response body-first 分类**

先解析 JSON 和 status/reason/skillCallId，再根据 HTTP status 判断：合法 rejected 即使非 2xx 也返回结构化结果；unknown reason 作为普通 rejected 保存；只有非法 body 才是 protocol failure。

- [ ] **Step 5: 实现 outbox retry**

claim 使用 `FOR UPDATE SKIP LOCKED`，生成 claimToken、复制 session fence 并原子递增 `attempt_count`。发送前再次检查 session current generation、cycle active、decision lease 等于 cycle latest lease，以及 frozen contract 中存在的 lease expiry；失效则 cancelled，不发 HTTP。每次重试发送数据库中的同一 raw bytes；可以刷新 requestId/timestamp/signature。状态迁移为 `planned/retryable_failed/过期 sending -> sending -> accepted/rejected`，所有回写使用 claimToken+fence CAS。`attempt_count >= DECISION_MAX_ATTEMPTS` 的过期 sending 由 recovery sweep 转为 dead_letter，不重新运行 Agent，避免 worker 在 HTTP 前连续崩溃导致无限 reclaim。

HTTP response 合并必须使用一个数据库事务：accepted 时先以 `decision_row_id + gateway_id + skill_call_id` 查/锁 call；call_skill/stop_hosting 原子更新 decision 为 accepted 并 upsert 同一个 pending call，wait/no_op 原子更新 decision 且禁止 call。若 skill event 已先建立 call，只允许相同 decisionId/skillCallId/skillName 合并；任一冲突将 decision/call 一起置 manual 或整笔回滚，绝不能出现 decision accepted 但 call 丢失。rejected 也在同一 CAS transaction 内保存稳定结果。

- [ ] **Step 6: 写并运行 sending crash/fencing RED 测试**

在 HTTP 调用前、响应后回写前设置可控 barrier；过期 sending 可被新 worker reclaim，旧 worker 迟到回写 CAS 失败。accepted 后崩溃重试仍发送相同 bytes，由 Gateway decisionId 幂等返回同一 skillCallId。

- [ ] **Step 7: 实现 heartbeat、有界 retry 和运行 GREEN**

decision retry 使用 Task 2 的 max attempts/base/max 配置和 PostgreSQL 时钟；每个 poll 更新 outbox heartbeat。

Run: `uv run pytest tests/unit/llm_gateway_v2/test_decision_client.py tests/unit/llm_gateway_v2/test_decision_worker.py -q`

Expected: PASS。

---

## Chunk 4: Readiness、日志、兼容上线与最终验证

### Task 13: 收紧日志和错误分类

**Files:**

- Create: `src/logging_config.py`
- Create: `src/core/integration/llm_gateway_v2/errors.py`
- Modify: `src/api/main.py`
- Modify: `src/core/infrastructure/db.py`
- Modify: `src/core/agents/nodes.py`
- Modify: `src/core/agents/decision_nodes.py`
- Modify: `src/core/agents/orchestrator.py`
- Modify: `src/core/engine/lightrag_engine.py`
- Modify: `src/core/integration/gateway_event_queue.py`
- Modify: `src/core/integration/robotgateway_callback.py`
- Modify: `src/api/routes/webhooks.py`
- Modify: `src/core/integration/llm_gateway_v2/event_service.py`
- Modify: `src/core/integration/llm_gateway_v2/event_worker.py`
- Modify: `src/core/integration/llm_gateway_v2/decision_service.py`
- Modify: `src/core/integration/llm_gateway_v2/decision_client.py`
- Modify: `src/core/integration/llm_gateway_v2/decision_worker.py`
- Create: `tests/unit/llm_gateway_v2/test_safe_logging.py`

- [ ] **Step 1: 写启动与 logger 顺序 RED 测试**

在导入数据库、Prefect、HTTP/LLM SDK 前必须先执行 `configure_logging()`；测试 monkeypatch module import 并断言敏感 logger level 已设置，SQL engine echo 为 false。

- [ ] **Step 2: 写五类失败日志 RED 测试**

分别注入事件处理、Agent/模型、数据库、decision callback 和启动依赖异常；异常文本包含 password/token/prompt/session snapshot/SQL 参数。断言日志只包含 stage、exception type、traceId/eventId/decisionId/skillCallId、耗时和 error category。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_safe_logging.py -q`

Expected: FAIL。

- [ ] **Step 4: 实现 typed errors、logger 初始化和安全日志**

禁止 `logger.exception` 直接记录外部异常；普通日志不使用 `%s, exc`。关闭默认 SQL echo，敏感 SDK logger 在依赖初始化前调整级别。

- [ ] **Step 5: 运行 GREEN 和 scoped Ruff**

Run: `uv run pytest tests/unit/llm_gateway_v2/test_safe_logging.py -q`

Expected: PASS。

Run: `uv run ruff check src/logging_config.py src/core/infrastructure/db.py src/core/agents/nodes.py src/core/agents/decision_nodes.py src/core/agents/orchestrator.py src/core/engine/lightrag_engine.py src/core/integration/gateway_event_queue.py src/core/integration/robotgateway_callback.py src/api/routes/webhooks.py src/core/integration/llm_gateway_v2 tests/unit/llm_gateway_v2/test_safe_logging.py`

Expected: 0 errors。

### Task 14: 实现外部依赖 readiness 和 capabilities gate

**Files:**

- Create: `src/core/integration/llm_gateway_v2/readiness.py`
- Create: `tests/api/test_readiness.py`
- Modify: `src/api/routes/gateway_v2.py`
- Modify: `src/api/main.py`
- Modify: `src/core/integration/llm_gateway_v2/worker_status.py`
- Modify: `src/core/integration/llm_gateway_v2/event_worker.py`
- Modify: `src/core/integration/llm_gateway_v2/decision_worker.py`
- Modify: `tests/unit/llm_gateway_v2/test_event_worker.py`

- [ ] **Step 1: 写 readiness RED 测试**

覆盖 DB revision 缺失、event worker heartbeat 过期、decision worker heartbeat 过期、v2 disabled 时两个 worker check 为 disabled/skipped 且不影响全局 ready、Embedding enabled/unavailable、Embedding disabled 不发请求、Rerank enabled/unavailable、Rerank disabled 不发请求、probe timeout、cache TTL/singleflight、全部 ready。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/api/test_readiness.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现 readiness probes**

- `ReadinessService` 构造函数注入 DB checker、两个 `WorkerStatusRegistry`、Embedding/Rerank probe、clock；并用 asyncio lock 合并并发 probe。
- DB：`SELECT 1`；通过 Alembic `ScriptDirectory.get_current_head()` 读取代码唯一 head，并要求数据库 `alembic_version.version_num` 精确相等，禁止字符串大小比较。
- Worker：registry 每个 poll loop 更新 monotonic 时间；fresh threshold=`max(3*POLL_MS, 2000ms)`，状态必须 running 且 heartbeat 未过期。
- v2 disabled：不启动 v2 workers，`eventWorker/decisionWorker` check 返回 `status=disabled`、`category=skipped`，top-level ready 计算把 disabled 视为非失败；不得伪造 heartbeat。v2 enabled 时仍必须是 running/fresh。
- Embedding：对实际配置的 adapter 执行最小探测并验证维度。
- Rerank：启用时执行最小 pair 探测；禁用时不构造 client、不发网络请求。
- 每个 probe 使用 `READINESS_TIMEOUT_SECONDS`；成功/失败快照缓存 `READINESS_CACHE_SECONDS`；配置 reload、worker stop 和 lifespan shutdown 主动失效缓存。
- 全局 `/ready` 由 `main.py` 注册，response 固定为 `{"status":"ready|not_ready","checks":{"database":...,"eventWorker":...,"decisionWorker":...,"embedding":...,"rerank":...}}`，检查项只含 status/category/checkedAtMs。

- [ ] **Step 4: capabilities gate**

`/api/gateway/v2/capabilities` 在 v2 disabled/not-ready 时返回 503；不得返回残缺 capabilities。

- [ ] **Step 5: 写运行期依赖失败恢复测试**

event processor 中 Embedding/Rerank 失败必须返回 retryable result；达到 event max attempts 后进入 dead_letter，不能生成 fallback wait 或把 event 标记 succeeded。

- [ ] **Step 6: 运行 GREEN**

Run: `uv run pytest tests/api/test_readiness.py tests/api/test_gateway_v2.py tests/unit/llm_gateway_v2/test_event_worker.py -q`

Expected: PASS。

### Task 15: 生命周期、兼容开关和关闭语义

**Files:**

- Modify: `src/api/main.py`
- Modify: `src/config.py`
- Create: `tests/api/test_gateway_v2_lifespan.py`
- Modify: `src/api/routes/gateway_v2.py`
- Modify: `src/api/routes/webhooks.py`
- Modify: `src/core/integration/llm_gateway_v2/event_worker.py`
- Modify: `src/core/integration/llm_gateway_v2/decision_worker.py`

- [ ] **Step 1: 写 lifespan RED 测试**

分别写四个 test 函数验证 v1/v2 开关矩阵。disabled 路由保持 OpenAPI 可见，但 runtime 请求固定返回 503 和 `service_disabled`。另用 barrier 写 shutdown test：停止 claim 新任务，在 `SHUTDOWN_GRACE_SECONDS` 内等待 in-flight；超时则取消 task，数据库 processing/sending lock 保留到 lockUntil 供下次 reclaim。

Run: `uv run pytest tests/api/test_gateway_v2_lifespan.py -q`

Expected: FAIL。

- [ ] **Step 2: 实现 worker lifecycle**

启动顺序：日志配置 -> DB/Redis -> provider -> v1 worker -> v2 event worker -> v2 decision worker -> readiness cache enable。关闭顺序：readiness not_ready -> workers draining -> grace wait/cancel -> v1 worker -> Redis/DB。`WorkerStatusRegistry` 状态必须依次为 starting/running/draining/stopped。

- [ ] **Step 3: 验证兼容矩阵**

| v1  | v2  | 预期                                       |
| --- | --- | ------------------------------------------ |
| on  | off | 现有 v1 可用，v2 503`service_disabled`。 |
| on  | on  | 两条独立路径可用。                         |
| off | on  | v1 503`service_disabled`，v2 可用。      |
| off | off | 两者都返回 503`service_disabled`。       |

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/api/test_gateway_v2_lifespan.py tests/api/test_gateway_v1_events.py tests/api/test_gateway_v2.py -q`

Expected: PASS。

### Task 16: CI 静态检查和测试隔离

**Files:**

- Modify: `tests/unit/test_decision_nodes.py`
- Modify: `tests/unit/test_nodes.py`
- Modify: `tests/conftest.py`
- Modify: `tests/unit/test_config.py`
- Modify: `src/api/routes/webhooks.py`
- Modify: `alembic/env.py`
- Modify: `alembic/versions/008_llm_gateway_v2_inbox.py`
- Modify: `alembic/versions/009_llm_gateway_v2_outbox.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_gateway_v2_migrations.py`
- Modify: `tests/integration/test_gateway_v2_recovery.py`
- Modify: `tests/integration/llm_gateway_v2/test_inbox_repository.py`
- Create: `.github/workflows/ci.yml`
- Modify: `pyproject.toml`

- [ ] **Step 1: 记录并修复明确基线**

当前 `uv run pytest tests/unit tests/api -q` 基线为 278 passed、1 failed，失败是 `test_nodes.py` 未包含 `gateway_skill_context`；先更新断言并验证原有 279 个测试通过。当前全仓 Ruff 有历史错误，本任务只按 remediation 修复两个点名测试文件和本次所有修改/新增文件，不扩大到无关文件，也不得用 ignore 掩盖本次问题。

- [ ] **Step 2: 增加 unit/API 禁止真实网络的 CI 命令**

增加 `pytest-socket` dev dependency。unit/API CI 使用无秘密环境，允许 `127.0.0.1/localhost/::1` 供 ASGI/Prefect 临时服务，禁止其它外部 socket。CI 对本计划新增/修改的生产文件、v2 tests、`test_decision_nodes.py` 和 `test_nodes.py` 运行 Ruff。

CI 另建 PostgreSQL service，数据库名固定为 `myagent_test_ci`，只把对应 URL 注入 `TEST_POSTGRES_DSN`；运行 migration 和 recovery integration tests。job 先用 `make_url()`/`SELECT current_database()` 双重断言测试库前缀，再允许 Alembic upgrade/downgrade。unit/API job 不继承该 DSN，integration job 不读取仓库 `.env`。

- [ ] **Step 3: 运行静态和测试基线**

Run: `uv run ruff check src/api/main.py src/api/routes/gateway_v2.py src/api/routes/webhooks.py src/config.py src/logging_config.py src/core/infrastructure/db.py src/core/agents/gateway_v2.py src/core/agents/gateway_v2_models.py src/core/agents/gateway_v2_prompts.py src/core/agents/nodes.py src/core/agents/decision_nodes.py src/core/agents/orchestrator.py src/core/engine/lightrag_engine.py src/core/integration/gateway_event_queue.py src/core/integration/robotgateway_callback.py src/core/integration/llm_gateway_v2 alembic/env.py alembic/versions/008_llm_gateway_v2_inbox.py alembic/versions/009_llm_gateway_v2_outbox.py tests/conftest.py tests/unit/test_config.py tests/unit/llm_gateway_v2 tests/api/test_gateway_v2.py tests/api/test_gateway_v2_lifespan.py tests/api/test_readiness.py tests/unit/test_decision_nodes.py tests/unit/test_nodes.py tests/integration/conftest.py tests/integration/test_gateway_v2_migrations.py tests/integration/test_gateway_v2_recovery.py tests/integration/llm_gateway_v2 .codex/skills/myagent2-sgai-http-e2e/scripts/simulation_driver.py .codex/skills/myagent2-sgai-http-e2e/scripts/simulation_myagent_app.py`

Run: `uv run pytest tests/unit tests/api -q`

Run: `if (-not $env:TEST_POSTGRES_DSN) { throw 'TEST_POSTGRES_DSN must target myagent_test_*' }; uv run pytest tests/integration/test_gateway_v2_migrations.py tests/integration/test_gateway_v2_recovery.py tests/integration/llm_gateway_v2/test_inbox_repository.py -q`

Expected: scoped Ruff 0 errors，unit/API 0 failures，真实 PostgreSQL integration 0 failures；非网络测试没有外部连接。全仓历史 Ruff 清理不作为 v2 capabilities 门禁。

### Task 17: 真实数据库恢复与 HTTP E2E

**Files:**

- Modify: `.codex/skills/myagent2-sgai-http-e2e/SKILL.md`
- Modify: `.codex/skills/myagent2-sgai-http-e2e/scripts/Invoke-MyAgent2SgaiHttpE2E.ps1`
- Modify: `.codex/skills/myagent2-sgai-http-e2e/scripts/simulation_driver.py`
- Modify: `.codex/skills/myagent2-sgai-http-e2e/scripts/simulation_myagent_app.py`
- Modify: `.codex/skills/myagent2-sgai-http-e2e/scripts/Test-MyAgent2SgaiHttpE2ESkill.ps1`
- Create: `scripts/seed_gateway_v2_test_tenant.py`
- Create: `scripts/invoke_gateway_v2_e2e.py`
- Create: `scripts/assert_gateway_v2_state.py`
- Create: `src/core/integration/llm_gateway_v2/worker_hooks.py`
- Modify: `src/core/integration/llm_gateway_v2/event_service.py`
- Modify: `src/core/integration/llm_gateway_v2/event_worker.py`
- Modify: `src/core/integration/llm_gateway_v2/decision_worker.py`
- Modify: `tests/integration/test_gateway_v2_recovery.py`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: 准备隔离数据库、迁移和 tenant**

`seed_gateway_v2_test_tenant.py` 和 `assert_gateway_v2_state.py` 只从 `TEST_POSTGRES_DSN` 读取连接，不接受命令行 DSN，启动时都使用 `make_url()` 强制数据库名以 `myagent_test_` 开头。seed 接受假 UUID tenant/gatewayId，不读取或写入 secret；assert 脚本只输出 ID、status、count 和 hash，不输出 JSONB/raw body。

Run:

```powershell
if (-not $env:TEST_POSTGRES_DSN) { throw 'TEST_POSTGRES_DSN is required' }
uv run python scripts/assert_gateway_v2_state.py --preflight-test-database
if ($LASTEXITCODE -ne 0) { throw 'unsafe TEST_POSTGRES_DSN' }
$env:POSTGRES_DSN = $env:TEST_POSTGRES_DSN
uv run alembic upgrade head
uv run python scripts/seed_gateway_v2_test_tenant.py --tenant-id 00000000-0000-0000-0000-000000000001 --gateway-id sgai-v2-e2e
uv run python scripts/assert_gateway_v2_state.py --expect-revision 009 --expect-empty
```

Expected: revision 为 009，sessions/cycles/events/decisions/skill_calls 五张 v2 runtime 表为空；任一非 `myagent_test_*` DSN 在迁移前失败。

- [ ] **Step 2: 先手工启动 myAgent2 并验证 capabilities**

凭证只通过当前 PowerShell process 的 `E2E_*` 环境变量提供。脚本和日志不得打印值。构造入站和出站两组不同身份，再启动真实 API：

```powershell
$required = 'E2E_EVENT_APP_ID','E2E_EVENT_APP_SECRET','E2E_DECISION_APP_ID','E2E_DECISION_APP_SECRET','E2E_GATEWAY_CONTROL_APP_ID','E2E_GATEWAY_CONTROL_APP_SECRET'
foreach ($name in $required) { $value = [Environment]::GetEnvironmentVariable($name, 'Process'); if ([string]::IsNullOrWhiteSpace($value)) { throw "$name is required" } }
$eventSecrets = @{}; $eventSecrets[$env:E2E_EVENT_APP_ID] = $env:E2E_EVENT_APP_SECRET
$eventGateways = @{}; $eventGateways[$env:E2E_EVENT_APP_ID] = @('sgai-v2-e2e')
$eventTenants = @{ 'sgai-v2-e2e' = '00000000-0000-0000-0000-000000000001' }
$gatewayContract = Get-Content tests\fixtures\llm_gateway_v2\gateway_runtime_config_keys.json -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$gatewayContract.decisionPath)) { throw 'Gateway decisionPath is not frozen' }
$env:LLM_GATEWAY_APP_SECRETS = $eventSecrets | ConvertTo-Json -Compress
$env:LLM_GATEWAY_APP_GATEWAYS = $eventGateways | ConvertTo-Json -Compress
$env:LLM_GATEWAY_APP_TENANTS = $eventTenants | ConvertTo-Json -Compress
$env:LLM_GATEWAY_DECISION_URL = "http://127.0.0.1:19091$($gatewayContract.decisionPath)"
$env:LLM_GATEWAY_DECISION_APP_ID = $env:E2E_DECISION_APP_ID
$env:LLM_GATEWAY_DECISION_APP_SECRET = $env:E2E_DECISION_APP_SECRET
$env:LLM_GATEWAY_V1_ENABLED = 'true'
$env:LLM_GATEWAY_V2_ENABLED = 'true'
$env:EMBEDDING_ENABLED = 'true'
$env:RERANK_ENABLED = 'true'
$myAgent = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList @('-m','uvicorn','src.api.main:app','--host','127.0.0.1','--port','8000') -WorkingDirectory $PWD -WindowStyle Hidden -PassThru
$ready = $null; $deadline = (Get-Date).AddSeconds(60)
try {
    do {
        try { $candidate = Invoke-RestMethod http://127.0.0.1:8000/ready -TimeoutSec 3; if ($candidate.status -eq 'ready') { $ready = $candidate } } catch { Start-Sleep -Milliseconds 500 }
    } while ($null -eq $ready -and (Get-Date) -lt $deadline)
    if ($null -eq $ready) { throw 'myAgent2 readiness timeout' }
    $capabilities = Invoke-RestMethod http://127.0.0.1:8000/api/gateway/v2/capabilities -TimeoutSec 3
    if ($capabilities.contractVersion -ne 'llm-gateway-http-v2') { throw 'unexpected capabilities contractVersion' }
    if ($capabilities.receiveEventsPath -ne '/api/gateway/v2/events') { throw 'unexpected capabilities receiveEventsPath' }
} catch {
    Stop-Process -Id $myAgent.Id -Force -ErrorAction SilentlyContinue
    throw
}
```

Expected: `/ready` 和 capabilities 都为 200；capabilities 的 events path 精确为 `/api/gateway/v2/events`。Real 模式不得禁用或 mock Embedding/Rerank。

- [ ] **Step 3: 构建/启动真实 SGAI 并驱动闭环**

先读取 Task 0 冻结的 `gateway_runtime_config_keys.json`。`Invoke-MyAgent2SgaiHttpE2E.ps1` 后续必须按该 fixture 的键名给 SGAI child process 注入 provider URL、contract version、capabilities/events/decision path、双向身份引用和 gatewayId；若 fixture 缺键立即失败，不回退到 v1 键或默认路径。

Run:

```powershell
$sgaiRoot = 'D:\Projects\游戏场景数据\SGAI'
$sgai = $null
try {
dotnet build "$sgaiRoot\DotNet\DotNet.sln" -c Debug --nologo
if ($LASTEXITCODE -ne 0) { throw 'SGAI build failed' }
$requiredBytes = 'robotgatewayllmconfigcategory.bytes','robotgatewayruntimeconfigcategory.bytes'
foreach ($name in $requiredBytes) { if (-not (Test-Path "$sgaiRoot\Config\Excel\cs\GameConfig\$name")) { throw "missing $name; run Unity export first" } }
$gatewayKeys = Get-Content tests\fixtures\llm_gateway_v2\gateway_runtime_config_keys.json -Raw | ConvertFrom-Json
$keyProperties = 'enabledKey','providerBaseUrlKey','contractVersionKey','capabilitiesPathKey','eventsPathKey','eventAppIdKey','eventAppSecretKey','gatewayIdKey','decisionAppIdKey','decisionAppSecretKey'
foreach ($property in $keyProperties) { if ([string]::IsNullOrWhiteSpace([string]$gatewayKeys.$property)) { throw "Gateway runtime fixture missing $property" } }
$gatewayRuntime = @{
    ([string]$gatewayKeys.enabledKey) = '1'
    ([string]$gatewayKeys.providerBaseUrlKey) = 'http://127.0.0.1:8000'
    ([string]$gatewayKeys.contractVersionKey) = 'llm-gateway-http-v2'
    ([string]$gatewayKeys.capabilitiesPathKey) = '/api/gateway/v2/capabilities'
    ([string]$gatewayKeys.eventsPathKey) = '/api/gateway/v2/events'
    ([string]$gatewayKeys.eventAppIdKey) = $env:E2E_EVENT_APP_ID
    ([string]$gatewayKeys.eventAppSecretKey) = $env:E2E_EVENT_APP_SECRET
    ([string]$gatewayKeys.gatewayIdKey) = 'sgai-v2-e2e'
    ([string]$gatewayKeys.decisionAppIdKey) = $env:E2E_DECISION_APP_ID
    ([string]$gatewayKeys.decisionAppSecretKey) = $env:E2E_DECISION_APP_SECRET
}
foreach ($entry in $gatewayRuntime.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, 'Process') }
$sgai = Start-Process -FilePath 'dotnet' -ArgumentList @("$sgaiRoot\Bin\App.dll",'--AppType=Server','--Process=20','--StartConfig=StartConfig/Localhost','--CreateScenes=1','--Console=0','--Develop=1') -WorkingDirectory "$sgaiRoot\Bin" -WindowStyle Hidden -PassThru
$gatewayReady = $false; $deadline = (Get-Date).AddSeconds(60)
do { $gatewayReady = Test-NetConnection 127.0.0.1 -Port 19091 -InformationLevel Quiet -WarningAction SilentlyContinue; if (-not $gatewayReady) { Start-Sleep -Milliseconds 500 } } while (-not $gatewayReady -and (Get-Date) -lt $deadline)
if (-not $gatewayReady) { throw 'SGAI Gateway startup timeout' }
New-Item -ItemType Directory -Force .codex/skills/myagent2-sgai-http-e2e/.run | Out-Null
uv run python scripts/invoke_gateway_v2_e2e.py --gateway-base-url http://127.0.0.1:19091 --gateway-id sgai-v2-e2e --output .codex/skills/myagent2-sgai-http-e2e/.run/v2-real-session.json
if ($LASTEXITCODE -ne 0) { throw 'Gateway v2 driver failed' }
uv run python scripts/assert_gateway_v2_state.py --session-file .codex/skills/myagent2-sgai-http-e2e/.run/v2-real-session.json --expect-complete-cycle
if ($LASTEXITCODE -ne 0) { throw 'Gateway v2 database assertion failed' }
} finally {
    if ($null -ne $sgai) { Stop-Process -Id $sgai.Id -Force -ErrorAction SilentlyContinue }
    if ($null -ne $myAgent) { Stop-Process -Id $myAgent.Id -Force -ErrorAction SilentlyContinue }
}
```

`invoke_gateway_v2_e2e.py` 负责 HMAC account-login-start、状态轮询、metrics 查询，并在第二次 decision accepted 后调用 Gateway 控制面 `/api/v1/hosting/stop` 触发确定性的 session 关闭；它只从 `E2E_GATEWAY_CONTROL_APP_ID/E2E_GATEWAY_CONTROL_APP_SECRET` 读取控制面凭证。成功顺序必须是：session_started -> first decision accepted -> skill_started -> skill_finished with next lease -> second decision accepted -> control stop -> session_stopped。输出 JSON 只包含 sessionId、gatewayId、controlGeneration、按 eventType 分组的 eventId、两个 decisionId、已观察到的 skillCallId 和执行前后 metrics，不包含请求 body 或凭证。

`assert_gateway_v2_state.py --expect-complete-cycle` 必须做唯一、不可宽松解释的断言：恰好一条目标 session 且为 stopped/currentGeneration 匹配；恰好一个目标 cycle 且为 stopped、`next_event_sequence=max(event_sequence)+1`；输出 JSON 中每个 eventId 在 inbox 恰好一条且全部 succeeded，其中 session_started/首次 skill_started/首次 skill_finished/session_stopped 各恰好一条、decision_rejected 为 0，其它 observation 也必须在输出中逐 ID 对上；不存在 processing/retryable_failed/dead_letter/manual。输出中的两个 decisionId 在 outbox 恰好各一条且均 accepted、source event/lease 唯一、`sha256(request_body_bytes)=body_hash`；不存在 planned/sending/retryable_failed/dead_letter/manual。首个 skillCallId 恰好一条、状态 succeeded、绑定唯一 skill_finished terminal；cycle 内不存在 pending/started call，控制 stop 收敛的其它 call 只能是 cancelled。Gateway metrics 使用前后差值断言 eventsFailed/decisionsRejected 均增加 0、decisionsAccepted 恰好增加 2。任何一边缺证据均失败。

- [ ] **Step 4: 正式定义 hooks 并注入恢复场景**

`worker_hooks.py` 定义并导出：

```python
class WorkerHooks(Protocol):
    async def after_event_commit(self, event_ids: tuple[str, ...]) -> None: ...
    async def before_agent(self, event_id: str) -> None: ...
    async def before_decision_http(self, decision_id: str) -> None: ...
    async def after_decision_http(self, decision_id: str) -> None: ...

class NoOpWorkerHooks:
    async def after_event_commit(self, event_ids: tuple[str, ...]) -> None:
        return None

    async def before_agent(self, event_id: str) -> None:
        return None

    async def before_decision_http(self, decision_id: str) -> None:
        return None

    async def after_decision_http(self, decision_id: str) -> None:
        return None

NO_OP_WORKER_HOOKS: WorkerHooks = NoOpWorkerHooks()
```

`EventService`、`EventWorker`、`DecisionWorker` 构造函数接受 `hooks: WorkerHooks = NO_OP_WORKER_HOOKS`。生产 lifespan 永远使用 no-op 实例，配置文件和环境变量不提供启用测试 hook 的入口；integration test 直接注入 barrier implementation。在 durable commit 后/ACK response 前、Agent 调用前、decision HTTP 前、Gateway response 后/数据库回写前四个窗口分别终止旧 task 或启动第二 worker，逐项断言 inbox/outbox/call status、attempt_count、decisionId、raw body hash、skillCallId、HTTP 调用次数和旧 claim CAS 失败。

Run: `uv run pytest tests/integration/test_gateway_v2_recovery.py -k "after_ack or during_agent or before_decision_http or after_gateway_accept" -q`

Expected: PASS，四个窗口均可从 PostgreSQL 恢复且不产生第二逻辑动作。

- [ ] **Step 5: 验证 gap 和旧 generation**

使用签名 HTTP 先发送 sequence 2，再发送 sequence 1；建立更高 generation 后发送旧 generation 迟到事件和旧 stop。`assert_gateway_v2_state.py --expect-gap-recovered --expect-old-generation-superseded` 必须验证新 generation 的 lease/context 未变化、旧事件没有 decision、回调数量没有增加。

Run: `uv run pytest tests/integration/test_gateway_v2_recovery.py -k "gap or generation" -q`

Expected: PASS。

- [ ] **Step 6: 闭环通过后再更新 E2E skill**

只有 Step 1-5 全绿后，skill 增加 `-ContractVersion v1|v2` 和 `-GatewayMode Simulation|Real`；v2 默认调用 `/api/gateway/v2/events`，显式保留 v1 模式。Simulation 使用真实 myAgent2 API/PostgreSQL/Redis、确定性 Agent adapter 和 mock SGAI，显式禁用 Embedding/Rerank 并用 socket spy 证明没有对应网络调用；Real 使用真实 Agent 依赖和 SGAI。skill 调用 seed/driver/assert 脚本，自测覆盖参数、migration、UUID tenant、两种 URL、数据库断言失败和缺少 Gateway runtime fixture。脚本不得伪造数据库成功状态，也不得把 mock readiness 当生产 readiness。

- [ ] **Step 7: 先运行 skill 自测**

Run: `.\.codex\skills\myagent2-sgai-http-e2e\scripts\Test-MyAgent2SgaiHttpE2ESkill.ps1`

Expected: PASS，参数矩阵、缺 fixture、非测试 DSN、数据库断言失败和 cleanup 路径均有测试证据；端口 8000/19091 无遗留 listener。

- [ ] **Step 8: 用 skill 复跑 Simulation 和 Real**

Simulation Run: `.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v2 -GatewayMode Simulation`

Expected: capabilities、两次 decision、skill terminal、session stopped 和数据库状态全部通过；`provesRealSgai=false`。

Real Run: `if (-not $env:SGAI_GATEWAY_BASE_URL) { throw 'SGAI_GATEWAY_BASE_URL is required' }; .\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v2 -GatewayMode Real -GatewayBaseUrl $env:SGAI_GATEWAY_BASE_URL`

Expected: 使用 Gateway Task 0 确认的 v2 配置键和真实 SGAI，完成同一闭环；输出中不出现凭证。

- [ ] **Step 9: 对 Task 17 新文件执行最终静态检查**

将以下同一组路径追加到 `.github/workflows/ci.yml` 的 scoped Ruff job；CI 和本地不得使用不同文件清单。

Run: `uv run ruff check src/core/integration/llm_gateway_v2/worker_hooks.py src/core/integration/llm_gateway_v2/event_service.py src/core/integration/llm_gateway_v2/event_worker.py src/core/integration/llm_gateway_v2/decision_worker.py tests/integration/test_gateway_v2_recovery.py scripts/seed_gateway_v2_test_tenant.py scripts/invoke_gateway_v2_e2e.py scripts/assert_gateway_v2_state.py .codex/skills/myagent2-sgai-http-e2e/scripts/simulation_driver.py .codex/skills/myagent2-sgai-http-e2e/scripts/simulation_myagent_app.py`

Expected: 0 errors。

---

## 2. 最终验证清单

- [ ] `uv run alembic upgrade head` 成功，当前 revision 为 009 或后续 revision。
- [ ] Task 16 和 Task 17 列出的 scoped Ruff 命令均为 0 errors；全仓历史 Ruff 单独治理。
- [ ] `uv run pytest tests/unit -q` 全部通过。
- [ ] `uv run pytest tests/api -q` 全部通过。
- [ ] `uv run pytest tests/integration/test_gateway_v2_migrations.py tests/integration/test_gateway_v2_recovery.py -q` 全部通过。
- [ ] v1 `/api/gateway/events` 回归通过。
- [ ] v2 `/api/gateway/v2/capabilities` 与 `/events` 合同测试通过。
- [ ] 单事件、混合 batch、duplicate、内容冲突、单项持久化失败 partial ACK 和 commit 失败全批 503 有证据。
- [ ] sequence gap、并行 session、新旧 generation 和 session_stopped 有证据。
- [ ] accepted/event 两种乱序、同 body retry、不同 body conflict 有证据。
- [ ] success、四类 failed、cancelled、timeout 和重复终态有证据。
- [ ] Agent/DB/callback 注入失败进入 retry/dead-letter，不静默成功。
- [ ] 日志扫描不包含 secret、完整 prompt、snapshot、SQL 参数或外部异常全文。
- [ ] Embedding/Rerank readiness 的 enabled/disabled/unavailable 矩阵通过。
- [ ] v2 capabilities 只在所有 mandatory feature ready 时可见。

## 3. 发布与回滚

### 发布顺序

1. 备份数据库并部署 migration 008/009。
2. 部署代码，保持 `LLM_GATEWAY_V2_ENABLED=false`。
3. 配置入站 AppId/gatewayId/UUID tenant 映射和独立出站 decision 身份。
4. 完成 readiness、contract、恢复和日志扫描。
5. 只为测试 Gateway 开启 v2；Gateway provider origin 指向 myAgent2，capabilities/events path 分别固定为 `/api/gateway/v2/capabilities` 和 `/api/gateway/v2/events`。
6. 观察 inbox/outbox/dead-letter/decision latency 指标。
7. 扩大 Gateway 范围，确认 v1 无新增流量后再关闭 v1。

### 回滚规则

- 首选关闭 v2 capabilities 和新事件接入，不回滚数据库 schema。
- 已 durable ACK 的 event/outbox 必须继续 drain 或转为 manual，不能因应用回滚删除。
- 只有所有 v2 inbox/outbox/call 均清空且完成备份后，才允许执行 migration downgrade。
- v1 路由和 worker 在完整迁移窗口内保持可用，作为显式配置的兼容路径，不自动 fallback。

## 4. 完成定义

只有 `LLM-V2-001` 至 `LLM-V2-012` 的自动化证据、真实 PostgreSQL 恢复测试和一次完整 HTTP E2E 全部通过后，才允许：

- 将 v2 capabilities 对正式 Gateway 返回 200；
- 把 Gateway 默认 provider 切换到 `/api/gateway/v2`；
- 宣称双方完成 v2 双向通信。
