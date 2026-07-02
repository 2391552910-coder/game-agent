# myAgent2 项目接口文档

本文档面向 myAgent2 与 RobotGateway / LLM Gateway 的运行时对接。当前主协议以 LLM Gateway HTTP v1 为准；旧版 `/webhooks/player-event` 与 `analysis.completed` callback 只作为兼容链路保留。

## 1. 项目职责边界

myAgent2 是 AI 游戏决策层，负责：

- 接收 Gateway 推送的托管事件。
- 基于玩家快照、行为事件、RAG 知识、历史记忆和行动追踪进行分析。
- 输出 Gateway 可消费的决策动作。
- 在 v1 主链路中，向 Gateway `/decision` 接口提交一次决策。
- 在旧版兼容链路中，离线分析完成后向 RobotGateway 回调 `analysis.completed`。

RobotGateway / LLM Gateway 负责：

- 账号托管、游戏服连接、session 生命周期管理。
- 状态采集、动作校验、skill 执行和最终结果回传。
- `decisionLeaseId` 发放、消费、过期和幂等控制。
- HMAC 身份校验、动作权限校验、skill 参数校验。

myAgent2 不负责：

- 直接调用游戏服底层协议。
- 托管账号 session。
- 执行 Gateway skill。
- 维护 Gateway 控制面 HMAC 签名。
- 把新协议输出成旧版 `action_type + payload`。

## 2. 通信总览

### LLM Gateway v1 主链路

```text
Gateway
  -> POST /api/gateway/events
  -> myAgent2 接收事件、验签、幂等、触发 Agent 分析
  -> POST /api/v1/hosting/llm/decision
  -> Gateway 校验 lease、skill、参数和状态，并执行或拒绝
  -> 后续通过 skill_finished / observation_updated / session_stopped 推进状态
```

| 方向 | 接口 | 角色 | 状态 |
|---|---|---|---|
| Gateway -> myAgent2 | `POST /api/gateway/events` | Gateway 推送托管事件 | v1 主接口 |
| myAgent2 -> Gateway | `POST /api/v1/hosting/llm/decision` | myAgent2 提交一次决策 | v1 主接口 |
| Gateway -> myAgent2 | `POST /webhooks/player-event` | 旧版玩家事件入口 | 兼容接口 |
| myAgent2 -> Gateway | `GET /players/{user_id}/snapshot` | 旧版缺少快照时拉取玩家快照 | 兼容接口 |
| myAgent2 -> Gateway | `POST <ROBOTGATEWAY_CALLBACK_URL>` | 旧版分析完成回调 | 兼容接口 |

## 3. v1 事件接口：Gateway -> myAgent2

### 3.1 Endpoint

```http
POST /api/gateway/events
Content-Type: application/json
X-AppId: <gateway-to-llm-app-id>
X-TimestampMs: <unix-epoch-ms>
X-RequestId: <request-id>
X-Signature: <hmac-sha256-hex>
```

项目中该接口由 `src/api/routes/webhooks.py` 的 `gateway_router` 提供，并在 `src/api/main.py` 中挂载到 `/api/gateway`。

### 3.2 Request Envelope

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `traceId` | 是 | string | Gateway 生成的链路追踪 ID。 |
| `gatewayId` | 是 | string | Gateway 实例 ID。 |
| `contractVersion` | 是 | string | 固定为 `llm-gateway-http-v1`。 |
| `event` | 是 | object | 本次推送的单个事件。 |

v1 每次只发送一个 `event`，不支持 `events[]` batch。

### 3.3 Event Common Fields

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `eventId` | 是 | string | 事件幂等 ID。 |
| `eventType` | 是 | string | `session_started`、`skill_finished`、`session_stopped`、`observation_updated`。 |
| `decisionLeaseId` | 按事件 | string | Gateway 发放的一次性决策许可。 |
| `occurredAtMs` | 是 | integer | 事件在 Gateway 内发生的毫秒时间戳。 |
| `payload` | 是 | object | 事件载荷。 |

`session_started`、`skill_finished`、`observation_updated` 必须携带 `decisionLeaseId`。`session_stopped` 不允许携带 `decisionLeaseId`。

### 3.4 Payload Blocks

`payload` 只允许携带当前事件需要的 block，不传无关 block，也不显式传 `null`。

| block | 说明 | 适用事件 |
|---|---|---|
| `session` | 当前 session 快照 | 全部事件 |
| `skill` | 已结束 skill 的终态摘要 | `skill_finished` |
| `stop` | session 停止原因 | `session_stopped` |
| `observation` | 新观察原因 | `observation_updated` |

#### `payload.session`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `sessionId` | 是 | string | Gateway 托管 session ID。 |
| `accountId` | 是 | string | 当前托管账号 ID。 |
| `roleId` | 是 | string | 当前被托管角色 ID。 |
| `sceneId` | 是 | integer | 当前场景配置 ID。 |
| `state` | 是 | string | `Running`、`Stopped`、`Failed`。 |
| `position` | 是 | object | `{ "x": number, "y": number, "z": number }`。 |
| `controllable` | 是 | boolean | 当前是否可操作。 |

#### `payload.skill`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `skillCallId` | 是 | string | Gateway 接受 `call_skill` 后生成的 skill 调用 ID。 |
| `skillName` | 是 | string | skill 名称。 |
| `reason` | 是 | string | skill 通用终态原因或已登记的技能专属 reason。 |

通用 reason：`ok`、`ttl_expired`、`cancelled`、`runtime_error`。

#### `payload.stop`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `reason` | 是 | string | `admin_stop`、`stop_hosting_requested`、`player_online`、`server_kicked`、`gateway_shutdown`、`runtime_error`。 |

#### `payload.observation`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `reason` | 是 | string | `wait_completed` 或 `state_changed`。 |

### 3.5 请求示例

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
        "accountId": "account-001",
        "roleId": "role-001",
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

### 3.6 接收响应

myAgent2 接收成功：

```json
{
  "status": "accepted",
  "eventId": "evt-001"
}
```

重复事件且 body hash 一致：

```json
{
  "status": "duplicate",
  "eventId": "evt-001"
}
```

请求级错误：

```json
{
  "error": {
    "code": "bad_request",
    "message": "bad request"
  }
}
```

`accepted` 只表示事件已通过验签、协议校验和幂等接收，并已进入 myAgent2 处理流程；不表示 Gateway skill 已执行成功。

## 4. v1 决策接口：myAgent2 -> Gateway

### 4.1 Endpoint

```http
POST /api/v1/hosting/llm/decision
Content-Type: application/json
X-AppId: <llm-to-gateway-app-id>
X-TimestampMs: <unix-epoch-ms>
X-RequestId: <request-id>
X-Signature: <hmac-sha256-hex>
```

本接口地址由 myAgent2 环境变量 `LLM_GATEWAY_DECISION_URL` 配置，例如本地联调可配置为：

```text
LLM_GATEWAY_DECISION_URL=http://127.0.0.1:9000/api/v1/hosting/llm/decision
```

### 4.2 Request Fields

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `contractVersion` | 是 | string | 固定为 `llm-gateway-http-v1`。 |
| `decisionId` | 是 | string | myAgent2 生成的决策幂等 ID。 |
| `decisionLeaseId` | 是 | string | Gateway 事件中发放的决策许可。 |
| `action` | 是 | string | `call_skill`、`wait`、`stop_hosting`。 |
| `skillName` | 按 action | string | `action=call_skill` 时必填。 |
| `schemaVersion` | 按 action | string | `action=call_skill` 时必填。 |
| `arguments` | 按 action | object | `action=call_skill` 时必填；`action=wait` 时可省略；`action=stop_hosting` 时不允许出现。 |

`/decision` 请求不携带 `sessionId`。Gateway 通过 `decisionLeaseId` 找到对应 session。

### 4.3 `call_skill`

myAgent2 当前最终决策模型输出 Gateway skill，再由 v1 适配层包装为：

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "decisionId": "decision-001",
  "decisionLeaseId": "lease-001",
  "action": "call_skill",
  "skillName": "move_to",
  "schemaVersion": "v1",
  "arguments": {
    "target": {
      "x": 20,
      "y": 0,
      "z": 30
    },
    "stopDistance": 1.5
  }
}
```

当前 myAgent2 开放的 Gateway skill：

| skillName | schemaVersion | arguments 约定 | 说明 |
|---|---|---|---|
| `observe_state` | `v1` | `{}` | 观察当前状态，不执行位移或动作。 |
| `move_to` | `v1` | `{ "target": { "x": number, "y": number, "z": number }, "stopDistance"?: number }` | 移动到目标坐标。 |
| `stop_move` | `v1` | `{}` | 停止移动。 |
| `jump` | `v1` | `{}` | 跳跃。 |
| `play_action` | `v1` | `{ "action": string }` | 播放或执行约定动作。 |

注意：

- HTTP v1 只规定 `arguments` 必须是 JSON object。
- 每个 skill 的精确参数字段、取值范围、默认值、可打断规则和执行 TTL 应以 Gateway skill 参数契约为准。
- myAgent2 当前代码侧强校验 `move_to.arguments.target.x/y/z` 必须是数字，`play_action.arguments.action` 必填。
- myAgent2 不在 `/decision` 中传通用 `ttlMs`；skill 执行 TTL 由 Gateway 内部配置。

### 4.4 `wait`

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

`arguments` 可省略，或传 `{}`。`waitMs` 是建议等待毫秒数，Gateway 负责限幅。

### 4.5 `stop_hosting`

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "decisionId": "decision-003",
  "decisionLeaseId": "lease-003",
  "action": "stop_hosting"
}
```

`stop_hosting` 不允许携带 `arguments`。Gateway 接受后应停止本次托管，并通过后续 `session_stopped(reason=stop_hosting_requested)` 表达终态。

### 4.6 Gateway 响应

接受：

```json
{
  "status": "accepted",
  "reason": "ok",
  "sessionId": "session-001",
  "skillCallId": "skill-call-001"
}
```

拒绝：

```json
{
  "status": "rejected",
  "reason": "lease_expired"
}
```

拒绝 reason：

| reason | 说明 |
|---|---|
| `lease_expired` | `decisionLeaseId` 已过期、已消费、被新 lease 替代，或 session 已终态。 |
| `schema_invalid` | 请求结构、action、schema 或 arguments 不合法。 |
| `skill_not_allowed` | 当前账号、角色、场景、权限或配置不允许调用该 skill。 |
| `state_not_allowed` | lease 有效，但当前业务状态不允许执行该 action。 |
| `skill_in_progress` | 当前执行中的 skill 不允许被本次动作打断或替换。 |
| `idempotency_key_conflict` | 同一个 `decisionId` 对应了不同 lease 或不同 body。 |

## 5. HMAC 认证、幂等与错误

### 5.1 双向 HMAC Header

v1 runtime HTTP 接口统一使用以下 header：

| Header | 必填 | 说明 |
|---|---|---|
| `X-AppId` | 是 | 调用方 AppId。 |
| `X-TimestampMs` | 是 | 毫秒时间戳字符串。 |
| `X-RequestId` | 是 | HTTP 请求幂等 ID。 |
| `X-Signature` | 是 | HMAC-SHA256 小写 hex 签名。 |

签名文本：

```text
method + "\n" + path + "\n" + timestampMs + "\n" + requestId + "\n" + bodySha256Hex
```

签名算法：

```text
signature = hmac_sha256(appSecret, signingText).hexLower()
```

`bodySha256Hex` 必须基于原始 HTTP request body bytes 计算，不做 JSON 规范化。

### 5.2 myAgent2 接收事件的幂等

myAgent2 对 Gateway 事件按 `eventId + bodySha256` 去重：

- 首次收到合法事件，返回 `status=accepted`。
- 重复收到相同 `eventId` 且 body hash 一致，返回 `status=duplicate`。
- 重复收到相同 `eventId` 但 body hash 不一致，返回 HTTP `400 bad_request`。

幂等记录 TTL 由 `LLM_GATEWAY_IDEMPOTENCY_TTL_SECONDS` 配置，默认 `86400` 秒。

### 5.3 myAgent2 提交决策的幂等

myAgent2 调 `/decision` 时生成 `decisionId`。如发生网络超时或连接中断，只能以相同 `decisionId + decisionLeaseId + bodySha256` 幂等重试，不能用同一张 lease 重新生成不同决策。

### 5.4 错误响应

请求级错误使用：

```json
{
  "error": {
    "code": "signature_invalid",
    "message": "request signature invalid"
  }
}
```

`error.code` 仅使用：`bad_request`、`signature_invalid`、`timestamp_expired`、`internal_error`。

决策级拒绝使用 HTTP `200 + status=rejected + reason` 表达，不使用请求级 `error` 结构。

## 6. Gateway skill 与 Tool Calling 的区别

myAgent2 中存在两类容易混淆的“工具/动作”：

| 类型 | 发生位置 | 作用 | 是否由 Gateway 执行 |
|---|---|---|---|
| Agent Tool Calling | Agent 分析阶段内部使用 | 查询历史、相似玩家、RAG、行动追踪、异常检测 | 否 |
| Gateway skill | Agent 最终决策输出 | 让 Gateway 执行游戏行为 | 是 |

当前 Agent 内部 Tool Calling 包括：

- `query_player_history`
- `query_similar_players`
- `dynamic_rag_query`
- `get_action_tracking`
- `detect_anomaly`

这些 Tool Calling 只用于 myAgent2 内部分析，不会直接发给 Gateway。

Agent 最终输出的 Gateway skill 形如：

```json
{
  "skillName": "observe_state",
  "schemaVersion": "v1",
  "arguments": {},
  "reason": "当前缺少可靠坐标或动作枚举，先观察状态",
  "priority": "medium",
  "ttlMs": 30000
}
```

v1 适配层向 Gateway `/decision` 提交时，会把该输出映射成：

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "decisionId": "decision-xxx",
  "decisionLeaseId": "lease-xxx",
  "action": "call_skill",
  "skillName": "observe_state",
  "schemaVersion": "v1",
  "arguments": {}
}
```

`reason`、`priority`、`ttlMs` 是 myAgent2 内部解释和追踪字段，不属于 v1 `/decision` 请求字段。

## 7. 旧版兼容接口

旧版接口仍用于已有 RobotGateway callback 架构或离线分析流程。新对接优先使用 LLM Gateway v1。

### 7.1 玩家事件入口

```http
POST /webhooks/player-event
Content-Type: application/json
X-API-Key: <tenant-api-key>
```

请求体：

```json
{
  "user_id": "user-001",
  "event_type": "offline",
  "timestamp": 1719999999.0,
  "snapshot": {
    "level": 10,
    "scene": "main_city"
  }
}
```

字段：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `user_id` | 是 | string | 玩家 ID。 |
| `event_type` | 是 | string | `online`、`offline`、`behavior_checkpoint`。 |
| `timestamp` | 是 | number | 事件时间戳。 |
| `snapshot` | 否 | object | 玩家快照。 |
| `session_id` | 条件 | string | `behavior_checkpoint` 必填。 |
| `behavior_event` | 条件 | object | 行为事件详情。 |

响应：

```json
{
  "status": "scheduled",
  "user_id": "user-001",
  "flow_run_id": "prefect-flow-run-id"
}
```

可能状态：

| status | 说明 |
|---|---|
| `scheduled` | 已调度离线分析。 |
| `debounced` | Redis 防抖命中，本次不重复调度。 |
| `cancelled` | 玩家上线，取消离线分析。 |
| `recorded` | 行为检查点已写入。 |

### 7.2 Snapshot 拉取

旧版流程中，如果事件未携带足够 snapshot，myAgent2 可从 RobotGateway 拉取：

```http
GET <ROBOTGATEWAY_BASE_URL>/players/{user_id}/snapshot
X-API-Key: <ROBOTGATEWAY_SNAPSHOT_API_KEY>
```

超时时间由 `ROBOTGATEWAY_SNAPSHOT_TIMEOUT_SECONDS` 配置。

### 7.3 分析完成回调

旧版分析完成后，myAgent2 可回调 RobotGateway：

```http
POST <ROBOTGATEWAY_CALLBACK_URL>
Content-Type: application/json
X-Callback-API-Key: <ROBOTGATEWAY_CALLBACK_API_KEY>
```

请求体：

```json
{
  "event_type": "analysis.completed",
  "tenant_id": "tenant-001",
  "user_id": "user-001",
  "timestamp": "2026-07-01T00:00:00+00:00",
  "snapshot": {},
  "analysis": {
    "player_profile": {
      "playstyle": "explorer",
      "current_goal": ["探索地图"],
      "bottlenecks": [],
      "engagement_level": "medium"
    },
    "recommended_actions": [
      {
        "skillName": "observe_state",
        "schemaVersion": "v1",
        "arguments": {},
        "reason": "缺少可靠坐标，先观察状态",
        "priority": "medium",
        "ttlMs": 30000
      }
    ]
  }
}
```

如果 `ROBOTGATEWAY_CALLBACK_URL` 未配置，myAgent2 会跳过 callback，不把它视为分析失败。

## 8. 环境变量配置

### 8.1 v1 主链路配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_GATEWAY_APP_SECRETS` | `{}` | myAgent2 接收 Gateway 事件时，用 `X-AppId` 查找 appSecret。 |
| `LLM_GATEWAY_APP_TENANTS` | `{}` | 将 `gatewayId` 映射为 myAgent2 tenant ID；未配置时使用 `gatewayId`。 |
| `LLM_GATEWAY_TIMESTAMP_TOLERANCE_MS` | `300000` | HMAC 时间戳容忍窗口，默认 5 分钟。 |
| `LLM_GATEWAY_IDEMPOTENCY_TTL_SECONDS` | `86400` | v1 事件幂等记录保留秒数。 |
| `LLM_GATEWAY_DECISION_URL` | `None` | myAgent2 调 Gateway `/decision` 的完整 URL。 |
| `LLM_GATEWAY_DECISION_APP_ID` | `None` | myAgent2 调 Gateway `/decision` 使用的 AppId。 |
| `LLM_GATEWAY_DECISION_APP_SECRET` | `None` | myAgent2 调 Gateway `/decision` 使用的 AppSecret。 |
| `LLM_GATEWAY_DECISION_TIMEOUT_SECONDS` | `10.0` | `/decision` HTTP 超时时间。 |

配置示例：

```env
LLM_GATEWAY_APP_SECRETS={"gateway-to-llm":"dev-gateway-secret"}
LLM_GATEWAY_APP_TENANTS={"gateway-01":"tenant-dev"}
LLM_GATEWAY_DECISION_URL=http://127.0.0.1:9000/api/v1/hosting/llm/decision
LLM_GATEWAY_DECISION_APP_ID=llm-to-gateway
LLM_GATEWAY_DECISION_APP_SECRET=dev-llm-secret
LLM_GATEWAY_DECISION_TIMEOUT_SECONDS=10
```

### 8.2 旧版兼容配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ROBOTGATEWAY_BASE_URL` | `None` | 旧版 snapshot 拉取的 Gateway 基础地址。 |
| `ROBOTGATEWAY_SNAPSHOT_API_KEY` | `None` | snapshot 拉取使用的 API Key。 |
| `ROBOTGATEWAY_SNAPSHOT_TIMEOUT_SECONDS` | `10.0` | snapshot 拉取超时时间。 |
| `ROBOTGATEWAY_CALLBACK_URL` | `None` | 旧版 `analysis.completed` 回调 URL。 |
| `ROBOTGATEWAY_CALLBACK_API_KEY` | `None` | 旧版 callback 请求头 `X-Callback-API-Key`。 |
| `ROBOTGATEWAY_CALLBACK_TIMEOUT_SECONDS` | `10.0` | 旧版 callback 超时时间。 |

## 9. 本地联调流程

### 9.1 前置服务

常见本地端口：

| 服务 | 地址 |
|---|---|
| myAgent2 API | `http://127.0.0.1:8000` |
| 模拟 RobotGateway | `http://127.0.0.1:9000` |
| Prefect API | `http://127.0.0.1:4200/api` |

常用启动命令：

```powershell
docker compose -f docker-compose.dev.yml up -d
uv run alembic upgrade head
scripts\run_api_robotgateway.cmd
scripts\run_analysis_flow_serve.cmd
```

### 9.2 v1 联调验收

1. myAgent2 API 启动后，`GET /health` 正常。
2. Gateway 使用正确 HMAC header 调 `POST /api/gateway/events`。
3. myAgent2 返回 `status=accepted`。
4. 重放完全相同事件，myAgent2 返回 `status=duplicate`。
5. 重放相同 `eventId` 但修改 body，myAgent2 返回 HTTP `400 bad_request`。
6. `session_started`、`skill_finished`、`observation_updated` 带 `decisionLeaseId` 时，myAgent2 能触发 Agent 分析。
7. `LLM_GATEWAY_DECISION_URL`、`LLM_GATEWAY_DECISION_APP_ID`、`LLM_GATEWAY_DECISION_APP_SECRET` 已配置时，myAgent2 会向 Gateway `/decision` 提交决策。
8. Gateway 返回 `accepted` 时，只表示已接受本次决策；skill 最终结果以后续 `skill_finished` 为准。
9. Gateway 返回 `rejected` 时，myAgent2 记录拒绝结果，等待 Gateway 后续新事件发放新的 `decisionLeaseId`。
10. `session_stopped` 不带 `decisionLeaseId`，myAgent2 不再对该 session 决策。

### 9.3 旧版链路验收

1. RobotGateway 调 `POST /webhooks/player-event` 并携带 `X-API-Key`。
2. `offline` 事件可以调度 Prefect deployment `analysis_flow/offline-analysis`。
3. `online` 事件可以取消离线分析。
4. `behavior_checkpoint` 携带 `session_id` 时写入 `session_events`。
5. 缺少 snapshot 时，myAgent2 可按配置拉取 snapshot。
6. `ROBOTGATEWAY_CALLBACK_URL` 已配置时，分析完成后发送 `analysis.completed`。
7. `ROBOTGATEWAY_CALLBACK_URL` 未配置时跳过 callback，分析流程本身不因此失败。

## 10. 对接注意事项

- 新对接应优先使用 LLM Gateway v1，不应再新增依赖旧版 callback 的主流程。
- v1 事件和决策都严格拒绝未知字段。
- 所有业务 ID 都按 string 处理，不要把数字 ID 作为 JSON number 传输。
- `decisionLeaseId` 是一次性业务许可，不是认证 token。
- `/decision` 一次只允许一个 action，不支持 action list 或多 skill batch。
- Gateway skill 的成功执行结果不看 `/decision` HTTP 200，而看 Gateway 后续 `skill_finished` 事件。
- 坐标、动作枚举、活动规则应来自 snapshot、RAG 或 Gateway skill contract；myAgent2 不应编造不可靠坐标或动作枚举。
- 找不到可靠动作参数时，myAgent2 应降级输出 `observe_state`。
- 生产环境应使用 HTTPS；本地或内网联调可按部署策略使用 HTTP。
