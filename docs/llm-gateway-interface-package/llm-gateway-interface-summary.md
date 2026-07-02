---
doc_type: brainstorm
status: confirmed
summary: Gateway 与 LLM HTTP 对接文档总入口；详细接口已拆分到事件、决策、鉴权幂等、技能草案和只读 Query 草案。
---

# LLM Gateway Interface Summary

这是 Gateway <-> LLM runtime HTTP 对接的总入口，可直接给 LLM 侧同事先看。详细字段、校验、重试、幂等和错误处理仍以拆分文档为准。

## 文档拆分

| 文档 | 内容 |
|---|---|
| `llm-gateway-event-api.md` | Gateway -> LLM `POST /api/gateway/events`，四类事件和接收响应。 |
| `llm-gateway-decision-api.md` | LLM -> Gateway `POST /api/v1/hosting/llm/decision`，三类 action 和响应语义。 |
| `llm-gateway-auth-idempotency.md` | HMAC、Header、ID、bodySha256、请求级错误、重试和幂等。 |
| `llm-gateway-interface-review.html` | 审核版 HTML，总览和可折叠细节，方便快速扫描。 |
| `llm-gateway-skill-contract-draft.md` | 后续单独讨论的 skill 参数表、结果 reason、TTL、可打断/抢占规则。 |
| `llm-gateway-query-contract-draft.md` | 后续单独讨论的只读 Query 接口，例如周围角色、角色详情、自身状态。 |

## Runtime 接口目录

| 方向 | 接口 | 作用 | 状态 |
|---|---|---|---|
| Gateway -> LLM | `POST /api/gateway/events` | Gateway 推送托管开始、技能结束、状态观察、托管终止事件。 | v1 主接口 |
| LLM -> Gateway | `POST /api/v1/hosting/llm/decision` | LLM 消费 `decisionLeaseId`，提交一次决策。 | v1 主接口 |
| LLM -> Gateway | `POST /api/v1/hosting/llm/query` | LLM 读取周围角色、自身状态、角色详情等只读信息。 | 后续单独讨论 |
| 运维 | `/health` | 部署探活。 | 可选，不属于 runtime contract |

v1 不定义 `/capabilities`。可用 skill、参数版本和权限由双方配置或离线契约维护，不走运行时探测。

## 已确认主线

- Gateway -> LLM 统一事件接口：`POST /api/gateway/events`。
- 事件请求体只带单个 `event` 对象，不使用 `events[]`。
- LLM -> Gateway 决策接口：`POST /api/v1/hosting/llm/decision`。
- 决策 action 首版只有 `call_skill / wait / stop_hosting`。
- Runtime 可用 skill 不通过事件下发，由双方配置或提前约定。
- `/decision.arguments` 必须按 `skillName + schemaVersion` 对应技能参数契约填写。
- LLM 主动查询 Gateway 信息后续单独作为只读 Query，不放进 `/decision`。
- Query 不消费 `decisionLeaseId`，不生成新 lease，不打断正在执行的 skill。
- `/decision` 是按 session 串行的写操作；后续 `/query` 是只读操作，可以并发读取快照，但不能修改状态。
- Gateway 和 LLM 双向 HTTP 都使用 HMAC，不额外交互 LLM token。
- `decisionLeaseId` 是一次性业务决策许可，不是认证 token。
- 同一个 session 同一时间最多只有一张有效 `decisionLeaseId`。
- 所有 runtime 协议对象都严格拒绝未知字段，不静默忽略。
- 生产环境必须使用 HTTPS；本地开发或内网联调可按部署配置例外使用 HTTP。
- 业务 ID 都是 opaque string，即使底层来源是 long，也不能用 JSON number 传输。
- 枚举值、字段名、字符串值都严格匹配，不做别名、大小写兼容或 trim。

## 事件边界

- `session_started`：托管已开始，LLM 可以做第一轮决策，必须带第一张 `decisionLeaseId`。
- `skill_finished`：已接受的 skill 进入终态，session 可继续决策时必须带下一张 `decisionLeaseId`。
- `observation_updated`：没有 skill 结果但需要给 LLM 下一轮决策机会时发送，必须带 `decisionLeaseId`。
- `session_stopped`：session 已终态，不带 `decisionLeaseId`，并且是同一 `sessionId` 的最后一个业务事件。

只要事件带 `decisionLeaseId`，就代表 Gateway 已确认当前 `session.state=Running` 且 `session.controllable=true`，LLM 可以提交一次新决策。

## 决策边界

- `/decision` 请求不带 `sessionId`，Gateway 通过 `decisionLeaseId` 找到 session。
- `/decision` 响应不返回 `nextDecisionLeaseId`；下一张 lease 只通过 Gateway -> LLM 事件发放。
- `call_skill` 被接受后才返回 `skillCallId`。
- `wait` 被接受后不回显 `waitMs`。
- `stop_hosting` 被接受后 Gateway 后续发送 `session_stopped(reason=stop_hosting_requested)`。
- 决策级拒绝通过 HTTP `200 + status=rejected + reason` 表达。
- 请求级错误通过 HTTP `400/401/429/500 + error` 表达。

## 最小示例

### Gateway -> LLM: session_started

```http
POST /api/gateway/events
Content-Type: application/json
X-AppId: gateway-to-llm
X-TimestampMs: 1719999999000
X-RequestId: req-gw-001
X-Signature: <64 lowercase hex>
```

```json
{
  "traceId": "trace-001",
  "gatewayId": "gateway-01",
  "contractVersion": "llm-gateway-http-v1",
  "event": {
    "eventId": "evt-001",
    "eventType": "session_started",
    "decisionLeaseId": "lease-001",
    "occurredAtMs": 1719999999000,
    "payload": {
      "session": {
        "sessionId": "session-001",
        "accountId": "3499620579203612672",
        "roleId": "3499620579203612673",
        "sceneId": 1001,
        "state": "Running",
        "position": {
          "x": 12.3,
          "y": 0,
          "z": 45.6
        },
        "controllable": true
      }
    }
  }
}
```

LLM 接收成功：

```json
{
  "status": "accepted",
  "eventId": "evt-001"
}
```

重复事件：

```json
{
  "status": "duplicate",
  "eventId": "evt-001"
}
```

### LLM -> Gateway: call_skill

```http
POST /api/v1/hosting/llm/decision
Content-Type: application/json
X-AppId: llm-to-gateway
X-TimestampMs: 1719999999500
X-RequestId: req-llm-001
X-Signature: <64 lowercase hex>
```

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "decisionId": "decision-001",
  "decisionLeaseId": "lease-001",
  "action": "call_skill",
  "skillName": "move_to",
  "schemaVersion": "v1",
  "arguments": {
    "x": 20,
    "y": 0,
    "z": 30
  }
}
```

Gateway 接受：

```json
{
  "status": "accepted",
  "reason": "ok",
  "sessionId": "session-001",
  "skillCallId": "skill-call-001"
}
```

Gateway 拒绝：

```json
{
  "status": "rejected",
  "reason": "lease_expired"
}
```

### LLM -> Gateway: wait

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "decisionId": "decision-002",
  "decisionLeaseId": "lease-002",
  "action": "wait",
  "arguments": {
    "waitMs": 3000
  }
}
```

`arguments` 可省略，`{}` 等同于省略。`waitMs` 默认 `3000`，最小 `1000`，最大 `10000`。

### LLM -> Gateway: stop_hosting

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "decisionId": "decision-003",
  "decisionLeaseId": "lease-003",
  "action": "stop_hosting"
}
```

Gateway 接受后取消当前执行中的 skill，并发送：

```json
{
  "traceId": "trace-001",
  "gatewayId": "gateway-01",
  "contractVersion": "llm-gateway-http-v1",
  "event": {
    "eventId": "evt-stop-001",
    "eventType": "session_stopped",
    "occurredAtMs": 1720000000000,
    "payload": {
      "session": {
        "sessionId": "session-001",
        "accountId": "3499620579203612672",
        "roleId": "3499620579203612673",
        "sceneId": 1001,
        "state": "Stopped",
        "position": {
          "x": 12.3,
          "y": 0,
          "z": 45.6
        },
        "controllable": false
      },
      "stop": {
        "reason": "stop_hosting_requested"
      }
    }
  }
}
```

### 请求级错误

请求级错误不进入业务处理，响应固定 JSON 结构：

```json
{
  "error": {
    "code": "signature_invalid",
    "message": "request signature invalid"
  }
}
```

`error.code` 仅允许 `bad_request / signature_invalid / timestamp_expired / internal_error`。

## 明确不做或已删除

- 不使用批量事件 `events[]`。
- 不在事件里下发 `goal`。
- 不在事件里下发 `availableSkills / skillArgumentHints`。
- 不在事件里携带 LLM token。
- 不在事件里携带 `message`。
- 不在事件里携带 `runningSkill / lastSkillResult / interruptedSkill`。
- 不提供 `skill_started` 事件。
- 不提供 `heartbeat` 事件。
- 不提供 `decision_rejected` 或 `lease_revoked` 事件。
- `/decision` 请求不携带 `sessionId / traceId / sourceEventId / stateVersion`。
- `/decision` 响应不返回 `nextDecisionLeaseId`。
- `/decision` 不支持 `confidence / reason / message / ttlMs`。
- `/decision` 不支持 action list、批量 skill 或部分成功。
- v1 不提供 `cancel_skill` action。
- Gateway 不因为 LLM 未决策而自动替 LLM 执行默认 skill。
- `gatewayVersion / buildVersion / LLM 服务版本` 不进入 runtime JSON body，只进日志或部署元数据。

## 后续单独讨论

- 首批 LLM 可调用 skill 清单。
- 每个 skill 的 `arguments / schemaVersion / skill_finished.reason / TTL / 可打断/抢占规则`。
- 自动射击、移动中打枪等复合行为应作为 canonical skill 讨论，不在 `/decision` action list 里展开。
- 只读 Query 的 `queryType / arguments / result / reason / 缓存和新鲜度`。
- 是否需要面向运维的可选 `/health` 实现细节。
