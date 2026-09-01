# myAgent2

myAgent2 是面向游戏托管角色的多租户 AI 决策服务。它接收 SGAI Gateway 的会话、场景和技能执行事件，通过 LangGraph/LangChain 调用 OpenAI 兼容的大模型生成决策，再以异步 HTTP 方式将决策回传给 Gateway。

当前对外联调以 **LLM Gateway HTTP V2** 为唯一业务契约，协议版本为：

```text
llm-gateway-http-v2
```

## 目录

- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [V2 HTTP 接口](#v2-http-接口)
- [事件类型](#事件类型)
- [HMAC 认证](#hmac-认证)
- [活动计划与决策流程](#活动计划与决策流程)
- [模型与 RAG](#模型与-rag)
- [环境要求](#环境要求)
- [配置](#配置)
- [本地启动](#本地启动)
- [生产 Docker 部署](#生产-docker-部署)
- [健康检查与监控](#健康检查与监控)
- [测试与代码检查](#测试与代码检查)
- [旧接口说明](#旧接口说明)
- [许可证](#许可证)

## 功能特性

- 以 LLM Gateway HTTP V2 为主的 Gateway 双向通信。
- 批量事件接收、事件幂等、事件内容冲突检测和异步事件处理。
- 基于 PostgreSQL 的事件 Inbox、决策 Outbox、技能调用和控制周期持久化。
- 基于 Redis Stream 的事件消费和多 worker 协作。
- 会话级控制代际、状态版本、决策租约和并发 fence，避免旧决策污染当前会话。
- 活动计划持久化，支持当前步骤推进、失败记录、有限重试和重新规划。
- 根据技能容量和场景状态对有限位置的活动进行控制，减少大量角色集中到同一活动。
- 支持 `call_skill`、`wait`、`no_op` 和 `stop_hosting` 四种 V2 决策动作。
- 支持技能开始、完成、失败、取消、超时和 Gateway 拒绝等终态处理。
- 支持机会式托管聊天事件，不伪造 Gateway 未公布的聊天技能。
- 支持 Token 按决策统计、进程累计统计和周期日志统计。
- 可选接入 LightRAG、Milvus、Neo4j、Redis 和 Ollama Embedding/Reranker。
- 提供 Docker Compose 开发环境和单机生产部署配置。

## 系统架构

```text
SGAI Gateway
    |
    | GET  /api/gateway/v2/capabilities
    | POST /api/gateway/v2/events
    |       + HMAC 请求头
    v
myAgent2 API
    |
    | 校验身份、Gateway、租户、事件版本和幂等性
    | 批量入库并立即返回 ACK
    v
PostgreSQL Inbox  ->  Redis Stream  ->  EventWorker
                                           |
                                           v
                                     DecisionWorker
                                           |
                              LangGraph + RAG + LLM Provider
                                           |
                                           v
                                  PostgreSQL Decision Outbox
                                           |
                                           | POST LLM_GATEWAY_DECISION_URL
                                           | + HMAC 请求头
                                           v
                                      SGAI Gateway
```

`POST /api/gateway/v2/events` 返回成功，只代表事件已经接收或被识别为幂等重复，不代表决策已经同步完成。事件和决策由后台 worker 异步处理，决策发送失败时按配置进行有限重试。

## V2 HTTP 接口

### 能力发现

```http
GET /api/gateway/v2/capabilities
```

Gateway 可通过该接口确认服务是否启用并读取实际能力。接口不使用 Gateway 事件的 HMAC 认证，但服务必须启用 V2 且通过 readiness 检查，否则返回 `503`。

典型响应：

```json
{
  "contractVersion": "llm-gateway-http-v2",
  "receiveEventsPath": "/api/gateway/v2/events",
  "supportedDecisionActions": [
    "call_skill",
    "wait",
    "no_op",
    "stop_hosting"
  ],
  "perEventAck": true,
  "controlGeneration": true,
  "eventSequence": true,
  "asyncSkillTerminal": true,
  "supportedEventTypes": [
    "session_started",
    "observation_updated",
    "skill_started",
    "skill_finished",
    "decision_rejected",
    "session_stopped"
  ],
  "maxEventBatchSize": 100,
  "maxDecisionTtlMs": 30000
}
```

实际的批量大小和决策 TTL 以服务响应为准。

### 事件接收

```http
POST /api/gateway/v2/events
Content-Type: application/json
X-AppId: <gateway-to-myagent-app-id>
X-TimestampMs: <unix-time-milliseconds>
X-RequestId: <unique-request-id>
X-Signature: <lowercase-hex-hmac-sha256>
```

请求体使用 `events[]` 批量结构，单批至少包含一个事件：

```json
{
  "traceId": "trace-001",
  "gatewayId": "gateway-prod-01",
  "contractVersion": "llm-gateway-http-v2",
  "sentAtMs": 1785897600000,
  "events": [
    {
      "eventId": "event-001",
      "eventType": "session_started",
      "sessionId": "session-001",
      "controlGeneration": 1,
      "eventSequence": 1,
      "stateVersion": 1,
      "decisionLeaseId": "lease-001",
      "occurredAtMs": 1785897599000,
      "payload": {
        "reason": "decision_requested",
        "lease": {
          "sessionId": "session-001",
          "controlGeneration": 1,
          "decisionLeaseId": "lease-001",
          "stateVersion": 1,
          "leaseKind": "hosting_control",
          "allowedActions": ["wait"],
          "allowedSkillName": null,
          "allowedSkillNames": [],
          "parentSkillName": null
        },
        "decisionContext": {
          "session": {
            "accountId": "account-001",
            "status": "active"
          },
          "availableSkills": [],
          "skillArgumentHints": []
        }
      }
    }
  ]
}
```

成功响应：

```json
{
  "accepted": true,
  "traceId": "trace-001",
  "receivedEventIds": ["event-001"],
  "duplicateEventIds": []
}
```

同一个 `eventId` 和相同内容再次提交时，事件会进入 `duplicateEventIds`。同一个 `eventId` 对应不同内容时，服务返回 `409 event_content_conflict`。

### 决策回传

决策由后台异步发送到 `LLM_GATEWAY_DECISION_URL` 配置的完整地址：

```http
POST <LLM_GATEWAY_DECISION_URL>
Content-Type: application/json
X-AppId: <myagent-to-gateway-app-id>
X-TimestampMs: <unix-time-milliseconds>
X-RequestId: <unique-request-id>
X-Signature: <lowercase-hex-hmac-sha256>
```

回传的 V2 公共字段包括：

```json
{
  "traceId": "trace-001",
  "contractVersion": "llm-gateway-http-v2",
  "sessionId": "session-001",
  "decisionId": "decision-001",
  "decisionLeaseId": "lease-001",
  "stateVersion": 2,
  "controlGeneration": 1,
  "ttlMs": 30000,
  "action": "call_skill",
  "skillName": "dance_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

`wait` 决策还会携带 `waitMs`。内部活动计划字段不会发送给 Gateway。Gateway 返回的 accepted/rejected 结果会写入决策投递和技能调用状态，用于后续幂等、重试和审计。

## 事件类型

| 事件类型 | 用途 |
| --- | --- |
| `session_started` | 建立或恢复一个托管会话，开启新的控制代际。 |
| `observation_updated` | 上报新的场景快照、角色状态或可决策上下文。 |
| `skill_started` | Gateway 已开始执行某个技能，只记录开始状态。 |
| `skill_finished` | Gateway 上报技能成功、失败、取消或超时，成功终态可推进活动计划。 |
| `decision_rejected` | Gateway 拒绝上一条决策，触发失败记录、重新规划或备用活动。 |
| `session_stopped` | 关闭会话，取消积压事件、未发送决策和未完成技能调用。 |
| `chat_received` | 接收托管角色收到的聊天消息。 |
| `nearby_friend_chat_requested` | 接入附近好友的机会式聊天流程。 |
| `chat_send_result` | 记录聊天发送结果。 |

`session_stopped` 会提升会话 fence。正在执行的模型调用返回后还会再次检查 fence，旧会话的模型结果不会继续创建或发送有效决策。没有停止事件时，服务也会根据会话空闲时间和事件最大允许年龄丢弃过期工作。

## HMAC 认证

事件入站和决策出站都使用以下四个请求头：

| 请求头 | 作用 |
| --- | --- |
| `X-AppId` | 标识调用方身份。 |
| `X-TimestampMs` | Unix 毫秒时间戳，用于限制重放。 |
| `X-RequestId` | 本次 HTTP 请求的唯一标识。 |
| `X-Signature` | 小写十六进制 HMAC-SHA256 签名。 |

签名原文由五行组成：

```text
UPPERCASE_HTTP_METHOD
REQUEST_PATH
X-TimestampMs
X-RequestId
SHA256_HEX_OF_RAW_BODY
```

计算规则：

```text
bodyHash = lowercase_hex(SHA256(raw_request_body_bytes))
signingText = METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + REQUEST_ID + "\n" + bodyHash
signature = lowercase_hex(HMAC_SHA256(appSecret, UTF8(signingText)))
```

签名必须基于实际发送的原始请求体字节计算。JSON 重新格式化、字段顺序变化或路径不一致都会导致签名失败。事件入站和决策出站建议使用不同的 AppId 和密钥。

## 活动计划与决策流程

V2 内部使用数据库持久化的活动计划，不把计划字段加入 Gateway 公共 HTTP 契约。计划包含目标、阶段、版本、当前步骤和步骤状态，动作和失败历史继续复用 decisions、skill calls 和 events 表。

典型流程：

```text
session_started
    -> 初始化或恢复活动计划
    -> 按当前场景和 lease 选择第一步
    -> 生成并回传 V2 decision
skill_started
    -> 只记录开始状态
skill_finished(success)
    -> 完成当前步骤
    -> 推进阶段和下一步骤
    -> 根据新快照生成下一条 decision
skill_finished(failed/cancelled/timeout)
    -> 记录失败历史
    -> retryable 失败有限重试
    -> 再次失败后跳过或重新规划
session_stopped
    -> 关闭计划并取消旧工作
```

计划只允许使用当前事件公布的技能、合法 `schemaVersion` 和 lease 授权范围。初始 Lobby 场景在条件满足时优先使用 `scene_tornado` 进入广场；进入广场后可按场景位置、活动容量、角色历史和模型选择执行活动。没有可执行技能时才使用 `wait` 或 `no_op`，`wait` 通过 `waitMs` 传递等待时长。

为避免多个角色长期选择完全相同的活动，系统会结合角色级动作历史、会话目标、场景状态和技能容量进行选择。有限位置活动会在数据库中进行并发预占，容量满时切换可用活动或等待。

## 模型与 RAG

### 决策模型

决策链路使用 OpenAI 兼容的 HTTP 接口，地址和模型由环境变量配置：

- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_DEFAULT_MODEL`
- `OPENAI_FAST_MODEL`

决策上下文会受边界限制，不会无限追加完整历史。V2 决策上下文包括会话和场景快照、当前 lease、可用技能、技能参数提示、活动计划、最近动作历史、最近失败历史以及受限的 RAG 内容。

### RAG 组件

项目包含以下可选组件：

- LightRAG：组织检索和上下文构建。
- Milvus：保存向量索引。
- Neo4j：支持实体和关系存储。
- Redis：支持精确匹配缓存和运行时队列。
- Ollama `qwen3-embedding:4b`：生成 Embedding。
- Ollama Qwen3 Reranker：可选重排序。
- PostgreSQL：保存业务、Gateway 事件、决策和活动计划状态。

RAG 通过 `RAG_*`、`LIGHTRAG_*`、`EMBEDDING_*` 和 `RERANK_*` 配置。模型上下文只注入受检索数量和 Token 上限约束的内容，不直接把全部场景文档发送给大模型。

## 环境要求

- Python `>=3.11,<3.13`。
- `uv`，用于依赖同步和命令执行。
- Docker Engine 和 Docker Compose。
- PostgreSQL 16 或兼容版本。
- Redis 7 或兼容版本。
- 使用 RAG 时需要 Milvus、etcd、MinIO；需要图存储时使用 Neo4j。
- 使用本地 Embedding/Reranker 时需要 Ollama 和对应模型。
- 一个可访问的 OpenAI 兼容大模型服务。

## 配置

### 本地配置

```powershell
Copy-Item .env.example .env
```

至少检查以下配置：

```env
LLM_GATEWAY_V1_ENABLED=false
LLM_GATEWAY_V2_ENABLED=true

LLM_GATEWAY_APP_SECRETS={"<gateway-to-myagent-app-id>":"<gateway-to-myagent-secret>"}
LLM_GATEWAY_APP_GATEWAYS={"<gateway-to-myagent-app-id>":["<gateway-id>"]}
LLM_GATEWAY_APP_TENANTS={"<gateway-id>":"<existing-tenant-uuid>"}

LLM_GATEWAY_DECISION_URL=http://<gateway-host>:<port>/<decision-path>
LLM_GATEWAY_DECISION_APP_ID=<myagent-to-gateway-app-id>
LLM_GATEWAY_DECISION_APP_SECRET=<myagent-to-gateway-secret>
LLM_GATEWAY_DECISION_TIMEOUT_SECONDS=10
```

配置关系如下：

- `LLM_GATEWAY_APP_SECRETS` 是 Gateway 调用 myAgent2 时使用的入站身份和密钥。
- `LLM_GATEWAY_APP_GATEWAYS` 把入站 AppId 限制到允许的 `gatewayId`。
- `LLM_GATEWAY_APP_TENANTS` 把 `gatewayId` 映射到数据库中已有的租户 UUID。
- `LLM_GATEWAY_DECISION_URL` 是 Gateway 提供的决策接收地址，不是 myAgent2 的 events 入站地址。
- `LLM_GATEWAY_DECISION_APP_ID` 和 `LLM_GATEWAY_DECISION_APP_SECRET` 用于 myAgent2 回传决策。

### V2 运行参数

`.env.example` 已提供完整配置模板。常用参数包括：

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| `LLM_GATEWAY_V2_MAX_EVENT_BATCH_SIZE` | `100` | 单次 events 最大事件数量。 |
| `LLM_GATEWAY_V2_AGENT_TIMEOUT_SECONDS` | `60` | 单次 Agent 决策上限。 |
| `LLM_GATEWAY_V2_AGENT_MAX_CONCURRENCY` | `16` | 模型决策共享并发上限。 |
| `LLM_GATEWAY_V2_EVENT_MAX_PARALLELISM` | `32` | 事件 worker 并行上限。 |
| `LLM_GATEWAY_V2_DECISION_MAX_PARALLELISM` | `16` | 决策投递并行上限。 |
| `LLM_GATEWAY_V2_DECISION_TARGET_SECONDS` | `55` | 决策目标处理时间。 |
| `LLM_GATEWAY_V2_LEASE_TTL_MS` | `600000` | 决策租约有效时间。 |
| `LLM_GATEWAY_V2_SESSION_IDLE_TIMEOUT_SECONDS` | `600` | 无新事件时的会话空闲保护。 |
| `LLM_GATEWAY_V2_EVENT_STALE_AFTER_SECONDS` | `480` | 事件超过该年龄后不再进入模型决策。 |
| `LLM_GATEWAY_V2_METRICS_LOG_INTERVAL_SECONDS` | `10` | V2 运行指标日志周期。 |

### 测试用强制决策

以下配置只用于联调或回放测试，生产环境应保持为空：

```env
LLM_GATEWAY_V2_FORCE_ACTION=
LLM_GATEWAY_V2_FORCE_WAIT_MS=10000
LLM_GATEWAY_V2_FORCE_SKILLS=
```

`LLM_GATEWAY_V2_FORCE_ACTION=wait` 会暂停所有动作决策，并使用 `LLM_GATEWAY_V2_FORCE_WAIT_MS` 作为等待时长。`LLM_GATEWAY_V2_FORCE_SKILLS` 使用逗号分隔的已公布技能名，服务会在符合 lease 和参数约束时用于测试技能选择。修改 `.env` 后必须完全停止 Uvicorn 进程再启动，单纯发送请求或热重载以外的进程重启不会替换已经加载的环境变量。

## 本地启动

安装依赖：

```powershell
uv sync
```

启动开发基础设施：

```powershell
docker compose -f docker-compose.dev.yml up -d postgres redis
docker compose -f docker-compose.dev.yml ps
```

如果启用完整 RAG 开发链路，可启动 Compose 文件中的 Neo4j、Milvus、etcd、MinIO、Ollama 等服务，并将 `.env` 中的连接地址和凭据与 Compose 配置保持一致。

执行数据库迁移：

```powershell
uv run alembic upgrade head
```

启动 API：

```powershell
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

Windows 下也可以使用项目脚本：

```powershell
.\scripts\run_api_robotgateway.cmd
```

局域网访问时使用运行服务主机的 IPv4 地址，例如：

```text
http://<myagent-lan-ip>:8000/api/gateway/v2/capabilities
http://<myagent-lan-ip>:8000/docs
```

确认 Windows 防火墙允许 TCP `8000` 入站，并确保 Gateway 能访问该地址。

## 生产 Docker 部署

生产 Compose 是单机部署配置，包含 API、迁移、PostgreSQL、Redis、Neo4j、Milvus、etcd、MinIO、Ollama 初始化和可选 RAG 导入服务。

创建生产配置：

```powershell
Copy-Item .env.prod.example .env.prod
```

修改 `.env.prod` 中的数据库密码、Redis 密码、Neo4j/MinIO 凭据、模型地址、模型密钥、Gateway HMAC 身份和租户 UUID。对局域网提供服务时设置：

```env
MYAGENT_API_BIND_ADDRESS=0.0.0.0
MYAGENT_API_PORT=8000
```

构建并启动：

```powershell
docker compose --env-file .env.prod -f docker-compose.prod.yml build
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
```

首次导入场景 RAG 文档时：

```powershell
docker compose --env-file .env.prod -f docker-compose.prod.yml --profile rag-import run --rm myagent-rag-import
```

生产镜像使用 Python 3.12、锁定的 `uv.lock` 安装依赖，以非 root 用户运行 API，容器内监听 `8000`。生产环境应在服务器防火墙、反向代理或网络 ACL 层限制访问，并优先使用 TLS。

## 健康检查与监控

基础探活：

```text
GET /health
GET /ready
GET /metrics
```

V2 运行监控：

```text
GET /api/gateway/v2/monitor
GET /api/gateway/v2/monitor/stream
```

重点关注：

- EventWorker、DecisionWorker 是否为 `ready`，以及 heartbeat 是否过期。
- events 入站 ACK 延迟、503 数量、事件队列积压和 dead letter 数量。
- Agent 获取并发槽是否饱和、模型调用耗时和超时数量。
- 决策 accepted/rejected、过期或被替换的旧决策数量。
- 每条 V2 决策的输入、输出和总 Token，以及当前进程和周期累计 Token。
- 活动容量占用、技能完成率和失败重试情况。

Token 统计只针对 Gateway V2 决策链路，既可查看 Prometheus 指标，也会按运行配置输出日志；不代表其他业务接口或独立模型调用的消耗。

## 测试与代码检查

安装开发依赖后，可运行：

```powershell
uv sync --dev
```

V2 API 和 readiness 测试：

```powershell
uv run pytest tests/api/test_gateway_v2.py tests/api/test_gateway_v2_lifespan.py tests/api/test_readiness.py -v
```

V2 单元、集成和模拟回放测试：

```powershell
uv run pytest tests/unit/llm_gateway_v2 tests/integration/test_activity_plan_runtime.py tests/integration/test_gateway_v2_recovery.py -v
```

代码检查：

```powershell
uv run ruff check src tests
uv run mypy src
```

真实双向 E2E 还需要同时具备可访问的 Gateway decision 接口、双方 HMAC 身份、有效测试租户、PostgreSQL、Redis 和可用模型服务。仅向 events 接口发送请求并收到 ACK，不能单独证明 decision 回传和技能终态已经闭环。

## 旧接口说明

项目代码中仍保留部分历史接口和兼容实现，包括：

- V1 Gateway 接口。
- 玩家 Webhook 与旧分析接口。
- 租户管理和配额接口。
- Provider 管理接口。
- 内部调试和历史集成接口。

这些接口当前不作为 V2 联调范围，README 也不公开其路径、参数和调用示例。这里的“隐藏”仅指文档展示范围，不等于删除代码，不会自动改变 FastAPI 路由注册和 OpenAPI 文档。新的 Gateway 联调统一使用 `/api/gateway/v2/capabilities` 和 `/api/gateway/v2/events`。

## 许可证

私有项目，未授权禁止使用。
