# myAgent2 - LLM Gateway HTTP v2

myAgent2 当前对外联调以 **LLM Gateway HTTP v2** 为唯一业务契约。SGAI Gateway 将会话和技能事件批量发送给 myAgent2，myAgent2 完成异步决策后，再把决策结果回送给 Gateway。

协议版本固定为：

```text
llm-gateway-http-v2
```

## 当前公开范围

当前 README 只公开以下 v2 接口：

| 方向                | 方法     | 路径或地址                                  | 用途                         |
| ------------------- | -------- | ------------------------------------------- | ---------------------------- |
| Gateway -> myAgent2 | `GET`  | `/api/gateway/v2/capabilities`            | 获取 v2 精确能力声明         |
| Gateway -> myAgent2 | `POST` | `/api/gateway/v2/events`                  | 批量提交会话、状态和技能事件 |
| myAgent2 -> Gateway | `POST` | `LLM_GATEWAY_DECISION_URL` 配置的完整地址 | 异步回送决策结果             |

`GET /health` 和 `GET /ready` 仅用于运维探活，不属于 Gateway 业务契约。

## 双向通信流程

```text
SGAI Gateway
    |
    | 1. GET /api/gateway/v2/capabilities
    | 2. POST /api/gateway/v2/events + HMAC headers
    v
myAgent2
    | 3. 校验身份、Gateway 白名单和租户映射
    | 4. 持久化事件并立即返回批次 ACK
    | 5. 后台按会话顺序处理事件并生成决策
    | 6. POST LLM_GATEWAY_DECISION_URL + HMAC headers
    v
SGAI Gateway
```

事件接口返回成功，只表示事件已被接收或识别为幂等重复，不表示决策已经同步完成。决策通过后台 outbox 独立投递，失败时按配置重试。

## 能力发现

```http
GET /api/gateway/v2/capabilities
```

该接口不要求 HMAC。服务必须启用 v2 且处于 ready 状态，否则返回 `503`。

响应示例：

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

`maxEventBatchSize` 和 `maxDecisionTtlMs` 来自服务端运行配置，Gateway 应以接口实际响应为准。

## 事件上报

```http
POST /api/gateway/v2/events
Content-Type: application/json
X-AppId: <gateway-to-myagent-app-id>
X-TimestampMs: <unix-time-milliseconds>
X-RequestId: <unique-request-id>
X-Signature: <lowercase-hex-hmac-sha256>
```

请求体是批次结构，`events` 至少包含一个事件：

```json
{
  "traceId": "trace-001",
  "gatewayId": "sgai-gateway-01",
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

同一个 `eventId` 和相同内容再次提交时，事件 ID 会进入 `duplicateEventIds`。同一个 `eventId` 对应不同内容时返回 `409 event_content_conflict`。

支持的事件类型：

| `eventType`           | 含义                                           |
| ----------------------- | ---------------------------------------------- |
| `session_started`     | 建立新控制代际，`eventSequence` 必须为 `1` |
| `observation_updated` | 状态或可决策上下文发生变化                     |
| `skill_started`       | Gateway 已开始执行技能                         |
| `skill_finished`      | 技能执行成功、失败、取消或超时                 |
| `decision_rejected`   | Gateway 拒绝了某个决策                         |
| `session_stopped`     | 当前托管会话停止                               |

所有字段均使用代码契约定义的 camelCase 名称。模型禁止额外字段；`sessionId`、`controlGeneration`、`stateVersion` 和 `decisionLeaseId` 必须与事件内 lease 保持一致。

## HMAC 认证

`POST /api/gateway/v2/events` 和 myAgent2 回送 Gateway 的 decision 请求都使用四个认证头：

| 请求头            | 作用                              |
| ----------------- | --------------------------------- |
| `X-AppId`       | 标识调用方身份                    |
| `X-TimestampMs` | Unix 毫秒时间戳，用于限制重放窗口 |
| `X-RequestId`   | 本次 HTTP 请求的唯一标识          |
| `X-Signature`   | 小写十六进制 HMAC-SHA256 签名     |

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
bodyHash   = lowercase_hex(SHA256(raw_request_body_bytes))
signingText = METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + REQUEST_ID + "\n" + bodyHash
signature  = lowercase_hex(HMAC_SHA256(appSecret, UTF8(signingText)))
```

签名必须基于实际发送的原始请求体字节计算。JSON 重新格式化、字段顺序变化或路径不一致都会导致签名失败。入站和出站必须使用不同的 AppId 和密钥。

## 运行要求

- Python `>=3.11,<3.13`
- `uv`
- PostgreSQL
- Redis
- 可用的 LLM Provider 配置
- v2 数据库迁移已执行到最新版本

## 配置

先创建本地配置：

```powershell
Copy-Item .env.example .env
```

以下是 v2 联调涉及的核心配置。占位值必须替换为部署环境的真实值，密钥不得提交到 Git：

```env
LLM_GATEWAY_V1_ENABLED=false
LLM_GATEWAY_V2_ENABLED=true

LLM_GATEWAY_APP_SECRETS={"<gateway-to-myagent-app-id>":"<gateway-to-myagent-secret>"}
LLM_GATEWAY_APP_GATEWAYS={"<gateway-to-myagent-app-id>":["<gateway-id>"]}
LLM_GATEWAY_APP_TENANTS={"<gateway-id>":"<existing-tenant-uuid>"}
LLM_GATEWAY_TIMESTAMP_TOLERANCE_MS=300000

LLM_GATEWAY_DECISION_URL=http://<gateway-host>:<port>/<gateway-decision-path>
LLM_GATEWAY_DECISION_APP_ID=<myagent-to-gateway-app-id>
LLM_GATEWAY_DECISION_APP_SECRET=<myagent-to-gateway-secret>
LLM_GATEWAY_DECISION_TIMEOUT_SECONDS=10

LLM_GATEWAY_V2_MAX_EVENT_BATCH_SIZE=100
LLM_GATEWAY_V2_MAX_DECISION_TTL_MS=30000
LLM_GATEWAY_V2_EVENT_MAX_ATTEMPTS=5
LLM_GATEWAY_V2_DECISION_MAX_ATTEMPTS=5
LLM_GATEWAY_V2_RETRY_BASE_MS=1000
LLM_GATEWAY_V2_RETRY_MAX_MS=300000
LLM_GATEWAY_V2_CLAIM_TTL_MS=30000
LLM_GATEWAY_V2_AGENT_TIMEOUT_SECONDS=30
LLM_GATEWAY_V2_POLL_MS=250
LLM_GATEWAY_V2_EVENT_MAX_PARALLELISM=4
LLM_GATEWAY_V2_DECISION_MAX_PARALLELISM=4
LLM_GATEWAY_V2_SHUTDOWN_GRACE_SECONDS=10
LLM_GATEWAY_V2_READINESS_TIMEOUT_SECONDS=3
LLM_GATEWAY_V2_READINESS_CACHE_SECONDS=5
```

配置关系必须满足：

- `LLM_GATEWAY_APP_SECRETS` 的键是 Gateway 调用 myAgent2 使用的入站 AppId。
- `LLM_GATEWAY_APP_GATEWAYS` 将入站 AppId 限制到允许使用的 `gatewayId`。
- `LLM_GATEWAY_APP_TENANTS` 将 `gatewayId` 映射到数据库中已存在的租户 UUID。
- `LLM_GATEWAY_DECISION_URL` 是 Gateway 提供的完整 decision 接收地址，不是 myAgent2 的入站路由。
- decision 出站 AppId、AppSecret 必须与 events 入站身份和密钥不同。

PostgreSQL、Redis 和 LLM Provider 的连接项继续使用 `.env.example` 中对应的占位配置。

## 安装与启动

同步依赖：

```powershell
uv sync
```

使用开发编排启动 v2 必需的 PostgreSQL 和 Redis：

```powershell
docker compose -f docker-compose.dev.yml up -d postgres redis
docker compose -f docker-compose.dev.yml ps
```

执行数据库迁移：

```powershell
uv run alembic upgrade head
```

启动 API：

```powershell
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

本机检查地址：

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/ready
http://127.0.0.1:8000/api/gateway/v2/capabilities
```

局域网联调时，将 `127.0.0.1` 替换为运行 myAgent2 的局域网 IPv4 地址，并确认 Windows 防火墙允许 TCP `8000` 入站。生产环境应通过反向代理、TLS 和网络访问控制暴露服务。

## 验证

运行 v2 API 契约测试：

```powershell
uv run pytest tests/api/test_gateway_v2.py tests/api/test_gateway_v2_lifespan.py tests/api/test_readiness.py -v
```

运行代码检查：

```powershell
uv run ruff check src tests
uv run mypy src
```

真实双向 E2E 必须同时具备可访问的 Gateway decision 接口、双方 HMAC 身份、测试租户、PostgreSQL 和 Redis。仅调用 events 并收到 ACK，不能单独证明 decision 回送链路已经闭环。

## 暂时隐藏的接口

项目代码中仍保留 v1 兼容接口、玩家 Webhook 与分析接口、租户与配额接口、Provider 管理接口，以及内部调试和历史接口。它们目前没有删除，但暂不在本 README 中公开路径、参数或调用示例，也不作为当前对外联调承诺。

本次“隐藏”仅指 README 的文档展示范围，不会自动删除 FastAPI 路由，也不会改变 OpenAPI/Swagger 的现有注册结果。当前新联调统一使用 LLM Gateway HTTP v2。

## 许可证

私有项目，未授权禁止使用。
