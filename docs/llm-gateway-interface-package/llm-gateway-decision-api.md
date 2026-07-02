---
doc_type: brainstorm
status: confirmed
summary: LLM -> Gateway 决策接口 `/api/v1/hosting/llm/decision` 的字段、动作、响应和拒绝理由。
---

# LLM Gateway Decision API

本文记录 LLM 向 Gateway 提交一次决策的接口。通用鉴权、签名、ID 格式、幂等和重试规则见 `llm-gateway-auth-idempotency.md`。

## Endpoint

```text
POST /api/v1/hosting/llm/decision
```

LLM 消费 Gateway 发给它的 `decisionLeaseId` 后，通过此接口提交一次决策。

## Request Fields

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `contractVersion` | 是 | string | 固定为 `llm-gateway-http-v1`。 |
| `decisionId` | 是 | string | LLM 侧生成的决策幂等 ID。 |
| `decisionLeaseId` | 是 | string | Gateway 最近一次发放的决策许可。 |
| `action` | 是 | string | `call_skill`、`wait`、`stop_hosting`。 |
| `skillName` | 按 action | string | `action=call_skill` 时必填。 |
| `schemaVersion` | 按 action | string | `action=call_skill` 时必填。 |
| `arguments` | 按 action | object | `action=call_skill` 时携带 skill 参数；`action=wait` 时可省略。 |

## Top-Level Rules

- `contractVersion` 缺失、不是字符串，或不等于 `llm-gateway-http-v1` 时，按请求级错误处理，返回 HTTP `400 bad_request`。
- `decisionId`、`decisionLeaseId` 格式和幂等规则见 `llm-gateway-auth-idempotency.md`。
- `/decision` 请求不携带 `sessionId`；Gateway 通过 `decisionLeaseId` 找到对应 session。
- 如果 `/decision` 请求带了 `sessionId`，按顶层未知字段处理，返回 `status=rejected / reason=schema_invalid`。
- 一次 `/decision` 只允许提交一个 action，不支持多 skill 批量、动作列表或部分成功。
- 如果需要组合行为，例如自动射击这类复合逻辑，应定义成一个 canonical skill，由 Gateway skill 内部执行；接口层不支持 action list。
- 顶层只允许当前 action 需要的字段；未知字段统一 `schema_invalid`。
- 同一个 `session` 同一时间最多只有一张有效 `decisionLeaseId`；Gateway 发出新的 `decisionLeaseId` 后，该 session 之前未消费的旧 lease 立即失效。
- 请求通过鉴权、JSON 解析，并且 `decisionLeaseId` 是当前有效 lease 后，Gateway 会消费这张 lease；后续即使返回决策级 rejected，也不能用同一张 lease 修改参数后重发。

## `action`

`action` 只能是以下三个值：

- `call_skill`
- `wait`
- `stop_hosting`

规则：

- 首版不提供 `cancel_skill`；如果 LLM 想换动作，应提交新的 `call_skill`，由 Gateway 按技能规则判断是否抢占旧 skill。
- `action` 必须是字符串。
- `action` 缺失、类型错误或不在枚举内时，按决策级错误处理，返回 `status=rejected / reason=schema_invalid`。
- `action` 错误会消耗当前 `decisionLeaseId`；LLM 不能用同一张 lease 修改后再发。

## `call_skill`

### 必需字段

- `skillName`
- `schemaVersion`
- `arguments`

### 规则

- `skillName` 只允许在 `action=call_skill` 时出现。
- `skillName` 必填，且必须是非空字符串。
- `skillName` 必须是技能参数表登记的 canonical skill name，不允许别名或同义词。
- `skillName` 缺失、类型错误或空字符串时，返回 `status=rejected / reason=schema_invalid`。
- `skillName` 是字符串但 Gateway 不支持或不允许调用时，返回 `status=rejected / reason=skill_not_allowed`。
- `schemaVersion` 只允许在 `action=call_skill` 时出现。
- `schemaVersion` 必填，且必须是非空字符串。
- `schemaVersion` 的值用于指向技能参数表里的某个版本，首版建议写成 `v1`、`v2` 这种格式。
- `schemaVersion` 绑定 `skillName` 解释，不是全局版本；例如 `move_to:v1` 和 `shoot:v1` 是两个不同参数契约。
- 某个 skill 升级参数契约时，只升级该 skill 自己的 `schemaVersion`，不影响其它 skill。
- Gateway 可以同时支持同一 skill 的多个 `schemaVersion`；LLM 发哪个版本，Gateway 就按哪个版本校验 `arguments`。
- Gateway 不支持的版本返回 `status=rejected / reason=schema_invalid`。
- 是否保留旧版本、保留多久，不进入接口字段，走双方配置和版本治理。
- `schemaVersion` 缺失、类型错误、空字符串，或对应不上该 `skillName` 的可用版本时，统一返回 `status=rejected / reason=schema_invalid`。
- `arguments` 必须是 JSON object，不允许字符串化 JSON。
- `arguments` 必填，并按 `skillName + schemaVersion` 对应的技能参数契约填写。
- 即使某个 skill 不需要参数，也必须传 `arguments: {}`。
- 缺少 `arguments` 时，返回 `status=rejected / reason=schema_invalid`。
- `arguments` 可以是空对象 `{}`，但只有对应 skill 参数契约允许无参数时才合法。
- `arguments` 里出现技能参数契约未定义的字段，返回 `status=rejected / reason=schema_invalid`。
- LLM 不允许传通用 skill 执行 TTL；每个 skill 的执行超时由 Gateway 按内部配置决定。

## `wait`

### 规则

- `arguments` 可省略。
- 如果传，只允许单个字段 `waitMs`。
- 空对象 `{}` 等同于省略。
- `waitMs` 是 LLM 建议 Gateway 等多久再给下一次观察；单位毫秒。
- `waitMs` 只控制 `action=wait` 后 Gateway 何时给下一次观察，LLM 可以建议，Gateway 负责限幅。
- `defaultWaitMs = 3000`。
- `minWaitMs = 1000`。
- `maxWaitMs = 10000`。
- 小于最小值时拉到最小值，大于最大值时压到最大值。
- `waitMs = 0` 按太短处理，拉到 `1000`。
- `waitMs` 为负数、`null`、字符串或小数时，均视为 `schema_invalid`。
- `wait` 被接受时，响应不回显 `waitMs`。
- 如果 `wait` 后等待期间 session 停止，Gateway 直接发 `session_stopped`，不必等待 `waitMs` 到期。
- 一次 `wait` 只对应下一次决策机会：到期发 `observation_updated(reason=wait_completed)`，中途状态变化发 `observation_updated(reason=state_changed)`，终态发 `session_stopped`。
- 同一次 `wait` 不允许先发送 `state_changed`，之后又补发 `wait_completed`。

## `stop_hosting`

### 规则

- `arguments` 不允许出现。
- 它只表示 LLM 请求结束本次托管，不表示交还后台或人工接管。
- `stop_hosting` 优先级最高，不受当前是否有 skill 执行中的限制。
- Gateway 接受 `stop_hosting` 后应取消当前执行中的 skill，不单独推送该 skill 的 `skill_finished(cancelled)`，随后发送 `session_stopped(reason=stop_hosting_requested)`。

## Decision Response

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `status` | 是 | string | `accepted` 或 `rejected`。 |
| `sessionId` | 否 | string | 能解析到对应 session 时返回；解析不到时可省略。 |
| `skillCallId` | 否 | string | `call_skill` 被接受后生成。 |
| `reason` | 是 | string | 接受或拒绝原因。 |

### Accepted

- `status=accepted` 时 `reason` 固定为 `ok`，包括 `call_skill`、`wait`、`stop_hosting`。
- `call_skill accepted` 表示 Gateway 已接受并会执行该 skill；不区分“已排队”和“已开始执行”。
- `skillCallId` 只在 `action=call_skill` 被接受时返回。
- `wait`、`stop_hosting` 和所有 rejected 响应都不返回 `skillCallId`。
- `call_skill` 的完整业务结果通过后续 `skill_finished` 事件返回给 LLM。
- LLM 收到 `accepted` 后，不主动补发 `/decision`，不轮询 Gateway，也不根据超时自行猜测 Gateway 状态；后续状态推进由 Gateway 通过 `skill_finished`、`observation_updated` 或 `session_stopped` 事件表达。

### Rejected

| reason | 说明 |
|---|---|
| `lease_expired` | `decisionLeaseId` 已过期、已失效、已消费、已被新 lease 替代，或 session 已终态。 |
| `schema_invalid` | 请求结构或 action/schema/arguments 不合法。 |
| `skill_not_allowed` | 技能维度不允许，例如账号、角色、场景、权限或配置没有开放该 skill。 |
| `state_not_allowed` | lease 有效，但当前 Running session 的业务状态不允许执行该 action。 |
| `skill_in_progress` | 当前执行中的 skill 不允许被本次新动作打断或替换。 |
| `idempotency_key_conflict` | 同一个 `decisionId` 对应了不同 `decisionLeaseId` 或不同 raw body bytes。 |

规则：

- `status=rejected` 时必须带固定枚举 `reason`，不带 `message`。
- 详细拒绝原因写 Gateway 日志，用 `traceId / decisionId / decisionLeaseId` 关联排查。
- `schema_invalid` 响应不返回 `field`、`message` 或字段级错误详情。
- `skill_not_allowed` 不用于参数格式错误、终态旧 lease 或当前状态不适合；这些分别归 `schema_invalid`、`lease_expired`、`state_not_allowed`。
- `state_not_allowed` 不用于终态旧 lease、不可控状态或缺字段错误；这些分别归 `lease_expired`、不发新 lease、`schema_invalid`。
- 当前执行中的 skill 是否能被新动作打断的细节不在本接口展开，交给技能参数/技能规则表维护。
- `status=rejected` 只是这次决策失败，不会自动补发新的 `decisionLeaseId`；LLM 要等 Gateway 后续通过新事件发 lease 再重新决策。

## HTTP Semantics

| 场景 | HTTP | 说明 |
|---|---|---|
| 决策被接受 | `200` | Gateway 已接受本次决策；skill 最终成败仍看后续事件。 |
| 决策业务拒绝 | `200` | lease、状态、schema 或业务状态不允许执行；用 `status=rejected` 表达。 |
| 幂等重复 | `200` | 返回第一次处理结果，不重复执行。 |
| JSON 解析失败、协议级必填字段缺失或类型错 | `400` | 请求没有进入可靠业务处理。 |
| HMAC 签名错、时间戳过期、身份不可信 | `401` | 不进入业务逻辑。 |
| Gateway 过载或限流 | `429` | 可选保护；不消费 `decisionLeaseId`。 |
| Gateway 内部异常 | `500` | 真异常；只有在业务处理和 lease 消费前失败时才不消费 `decisionLeaseId`。 |

## Decision Lease Lifecycle

- `decisionLeaseId` 只能消费一次。
- LLM 使用已被新 lease 替代的旧 lease 提交 `/decision`，返回 `status=rejected / reason=lease_expired`。
- lease 作废不单独发送事件；只通过新事件里的新 `decisionLeaseId`，以及旧 lease 后续被使用时返回 `lease_expired` 来体现。
- 每张 `decisionLeaseId` 有 Gateway 内部等待超时时间，LLM 不传、也不控制，具体值走 Gateway 配置，不进入接口字段。
- LLM 在 lease 等待超时后才提交 `/decision`，返回 `status=rejected / reason=lease_expired`。
- Gateway 已经把带 `decisionLeaseId` 的事件成功投递给 LLM 后，如果 LLM 长时间不提交决策，达到 Gateway 内部等待上限时，Gateway 应结束本次托管 session，发送 `session_stopped(reason=runtime_error)`，`payload.session.state=Failed`。
- Gateway 不因为 LLM 未决策而自动替 LLM 执行默认 skill，也不自动生成新 lease 反复催促。
- 唯一例外是同一个 `decisionId + decisionLeaseId + bodySha256` 的 HTTP 幂等重试，应返回第一次处理结果。
- 如果 `decisionLeaseId` 是当前有效 lease，则 `schema_invalid`、`skill_not_allowed`、`state_not_allowed`、`skill_in_progress`、`idempotency_key_conflict` 这些决策级拒绝都会消费该 lease。
- 以下场景不消费当前有效 lease：鉴权失败、JSON 解析失败、`contractVersion / decisionId / decisionLeaseId` 这些协议根字段缺失或格式错误、以及完全相同的幂等重试。
- 如果 Gateway 在消费 lease 前因内部异常返回 HTTP `500`，该 lease 不消费，LLM 可以按原请求幂等重试。
- 如果 Gateway 已经消费 lease 并进入 action 处理，应尽量返回确定的 `accepted/rejected`；不要在业务已接管后用纯 HTTP `500` 丢失结果语义。
- Gateway 只有在能确定后续返回结果语义时，才应消费 lease。
- LLM 不能用同一张 lease 反复试错；需要等待 Gateway 后续通过事件发放新的 `decisionLeaseId`。

## Retry Reminder

- LLM -> Gateway 决策重试时，`decisionId + decisionLeaseId + bodySha256` 必须保持不变；LLM 应复用完全相同的 JSON body bytes，字段顺序、空格和数字格式都不应改变。
- LLM 只有在 `/decision` HTTP 没拿到响应、连接中断或网络超时时，才按原请求幂等重试；如果已经拿到 `accepted/rejected`，就等待 Gateway 后续事件。
