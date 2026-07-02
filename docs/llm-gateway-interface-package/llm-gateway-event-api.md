---
doc_type: brainstorm
status: confirmed
summary: Gateway -> LLM 统一事件接口 `/api/gateway/events` 的字段、事件类型和接收响应规则。
---

# LLM Gateway Event API

本文记录 Gateway 推给 LLM 的统一事件接口。通用鉴权、签名、ID 格式、幂等和重试规则见 `llm-gateway-auth-idempotency.md`。

## Endpoint

```text
POST /api/gateway/events
```

首版所有 Gateway -> LLM 通知都走这一条接口，通过 `event.eventType` 区分事件语义。

v1 不使用 `events[]`，每次请求只携带单个 `event`。不支持 batch，也不存在部分成功。

## Request Envelope

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `traceId` | 是 | string | 本次事件推送链路 ID。 |
| `gatewayId` | 是 | string | Gateway 实例 ID。 |
| `contractVersion` | 是 | string | 固定为 `llm-gateway-http-v1`。 |
| `event` | 是 | object | 本次推送的单个事件。 |

规则：

- `traceId / gatewayId` 格式和用途见 `llm-gateway-auth-idempotency.md`。
- `contractVersion` 缺失、不是字符串，或不等于 `llm-gateway-http-v1` 时，LLM 返回 HTTP `400 bad_request`。
- envelope 顶层只允许 `traceId / gatewayId / contractVersion / event`。
- envelope 结构错误不进入事件去重，也不返回 `accepted/duplicate`。

## Event Common Fields

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `eventId` | 是 | string | 事件唯一 ID，LLM 必须按它幂等去重。 |
| `eventType` | 是 | string | `session_started`、`skill_finished`、`session_stopped`、`observation_updated`。 |
| `decisionLeaseId` | 按事件 | string | Gateway 发给 LLM 的下一次决策许可。 |
| `occurredAtMs` | 是 | integer | 事件在 Gateway 内发生的 Unix epoch milliseconds。 |
| `payload` | 是 | object | 事件载荷。 |

规则：

- event 对象只允许 `eventId / eventType / decisionLeaseId / occurredAtMs / payload`。
- `occurredAtMs` 表示业务事件发生时间，不是 HTTP 发送时间；HTTP 发送和签名时间使用 header `X-TimestampMs`。
- `occurredAtMs` 必须是 Unix epoch milliseconds 的 integer number，不允许字符串、小数、`null` 或空值。
- HMAC 防重放只校验 `X-TimestampMs`，不因 `occurredAtMs` 与当前时间差异较大而拒绝事件；`occurredAtMs` 明显异常时最多写日志。
- 本事件接口的协议对象只允许文档明确列出的字段；多传字段、缺必填、类型错、层级错或 block 组合错，统一 HTTP `400 bad_request`。

## Payload Blocks

`payload` 只允许以下 canonical blocks。每个事件只带与自己有关的 block，其他 block 省略，不传 `null`。

| block | 含义 | 适用事件 |
|---|---|---|
| `session` | 当前最新 session 快照。 | 全部事件 |
| `skill` | 与一次 skill 调用相关的终态摘要。 | `skill_finished` |
| `stop` | session 终止原因。 | `session_stopped` |
| `observation` | 新观察原因。 | `observation_updated` |

已删除或不在事件里携带：

- `payload.decision`
- `interruptedSkill`
- `heartbeat`
- `availableSkills`
- `skillArgumentHints`
- `lastSkillResult`
- `runningSkill`
- `goal`
- LLM token
- `message`

## Event Type Matrix

| eventType | 顶层 `decisionLeaseId` | payload blocks | 说明 |
|---|---|---|---|
| `session_started` | 必填 | `session` | Gateway 已开始托管，LLM 可以做第一轮决策。 |
| `skill_finished` | 必填 | `session + skill` | 已接受的 skill 已进入终态，LLM 可以做下一轮决策。 |
| `session_stopped` | 不允许 | `session + stop` | session 已终态，LLM 不再对这个 session 决策。 |
| `observation_updated` | 必填 | `session + observation` | Gateway 给 LLM 一次新的观察和下一轮决策机会。 |

只要事件带 `decisionLeaseId`，就代表 Gateway 已确认当前 `session.state=Running` 且 `session.controllable=true`，LLM 可以提交一次新决策。

同一个 session 同一时间最多只有一张有效 `decisionLeaseId`。Gateway 发出新的 `decisionLeaseId` 后，该 session 之前未消费的旧 lease 立即失效。lease 作废不单独发送事件。

## Event Ordering

- v1 不增加 `sequence` 字段。
- Gateway 对同一个 `sessionId` 的事件必须串行投递。
- 同一个 session 任意时刻最多只有一个未完成投递的 Gateway -> LLM 事件。
- 前一个事件未成功投递前，Gateway 不发送同一 session 的后一个事件。
- 不同 session 之间不保证事件顺序，各自独立推进。
- LLM 不需要按 `occurredAtMs` 对同一 session 事件重新排序，也不处理乱序状态回滚。
- LLM 收到旧事件的重复投递时，只按 `eventId + bodySha256` 返回 `duplicate`，不能把内部 session 状态回滚到旧事件。

## `payload.session`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `sessionId` | 是 | string | Gateway 托管 session ID。 |
| `accountId` | 是 | string | 当前托管账号 ID，长期保留并传给 LLM。 |
| `roleId` | 是 | string | 当前被托管角色 ID。 |
| `sceneId` | 是 | integer | 当前场景配置 ID。 |
| `state` | 是 | string | `Running`、`Stopped`、`Failed`。 |
| `position` | 是 | object | `{ "x": number, "y": number, "z": number }`。 |
| `controllable` | 是 | boolean | 当前是否可操作。 |

规则：

- `payload.session` 只允许 `sessionId / accountId / roleId / sceneId / state / position / controllable` 七个字段。
- `accountId` 必填且长期保留；账号相关权限由 Gateway 控制，LLM 不因拿到 `accountId` 获得账号管理权限。
- `sceneId` 必须是 JSON number 里的整数值，不允许字符串、小数、`null` 或空值。
- `position` 必填，不传 `null`；必须是只包含 `x / y / z` 三个 number 字段的 object。
- `position.x / position.y / position.z` 必须是合法有限 JSON number，不允许字符串、`null`、`NaN`、`Infinity` 或 `-Infinity`。
- v1 不在接口层声明坐标地图边界范围；坐标是否越界由 Gateway 内部保证和记录。
- 坐标单位不随事件传输，默认双方按游戏世界坐标约定理解。
- `state=Running` 时，`position` 表示当前或最后一次已知位置。
- `state=Stopped` 或 `state=Failed` 时，`position` 表示停止前最后一次已知位置。
- `state=Running` 表示托管 session 仍存活，但不代表一定可操作；是否能决策看 `decisionLeaseId` 和 `controllable`。
- `session_started` 里 `controllable` 必须为 `true`。
- `skill_finished` 和 `observation_updated` 都带 `decisionLeaseId`，因此 `controllable` 必须为 `true`。
- `session_stopped` 里 `controllable` 必须为 `false`。

## `session_started`

含义：Gateway 已完成账号登录、进入托管运行态，并通知 LLM 开始接管决策。它不是账号登录接口，也不要求 LLM 返回登录 token。

规则：

- 必须带第一张 `decisionLeaseId`。
- `payload.session.state` 必须为 `Running`。
- `payload.session.controllable` 必须为 `true`。
- 不携带 `goal`；托管目标和策略由 LLM 侧自行决定。
- 如果 Gateway 暂时还不能让 LLM 决策，例如角色未进场、不可控或状态未准备好，就先不要发送 `session_started`，等准备好后再发送。

## `skill_finished`

含义：LLM 上一次提交并被 Gateway 接受的 `call_skill` 已进入终态，Gateway 把执行结果、最新 session 快照和下一张决策 lease 推给 LLM。

规则：

- 必须带 `decisionLeaseId`。
- `payload` 只带 `session + skill`。
- 如果 skill 结束后 session 已停止，不发送 `skill_finished`，应发送 `session_stopped`。
- 如果 skill 结束后 session 仍存活但暂时不可控，不立即发送 `skill_finished`；等恢复可控后再发送，或如果期间进入终态则发送 `session_stopped`。
- `skill_finished` 只用于已被 Gateway 接受执行的 skill；如果决策本身没有被接受，只在 `/decision` 响应里返回 `status=rejected`，不生成 skill 终态。
- 抢占导致旧 skill 被取消时，不单独向 LLM 推送旧 skill 的 `skill_finished(cancelled)` 事件；旧 skill 的取消细节写 Gateway 日志。
- `skill_finished.reason=cancelled` 保留给普通取消场景使用：取消后 session 仍继续，且 Gateway 要把这次取消作为下一轮决策输入。

### `payload.skill`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `skillCallId` | 是 | string | Gateway 接受 `call_skill` 后生成的 skill 调用 ID。 |
| `skillName` | 是 | string | skill 名称，例如 `move_to`。 |
| `reason` | 是 | string | skill 通用终态原因或已登记的技能专属 reason。 |

规则：

- `payload.skill` 只允许 `skillCallId / skillName / reason` 三个字段。
- `skillCallId` 格式见 `llm-gateway-auth-idempotency.md`。
- `skill_finished.payload.skill.skillCallId` 必须对应前一次 `/decision action=call_skill accepted` 响应返回的同一次 skill 调用。
- `skill_finished.payload.skill.skillName` 必须等于该 `skillCallId` 创建时的 `skillName`；Gateway 内部如果发生新 skill 替换，应生成新的 `skillCallId`。
- `reason` 必须来自通用 skill reason 枚举，或已在技能参数/结果表登记的技能专属 reason；未登记的 reason 返回 HTTP `400 bad_request`。

通用 skill reason：

| reason | 说明 |
|---|---|
| `ok` | skill 成功完成。 |
| `ttl_expired` | Gateway 配置的 skill 执行 TTL 到期。 |
| `cancelled` | skill 被取消。 |
| `runtime_error` | skill 执行期间发生 Gateway 内部或外部依赖错误。 |

单个 skill 的业务失败原因后续由技能参数表维护；例如移动类 skill 的 `target_unreachable` 可以作为技能专属 reason 登记，但必须登记后才能发给 LLM。

## `session_stopped`

含义：当前托管 session 已进入终态，LLM 不应再对同一个 `sessionId` 提交任何决策。重新托管、重新登录或重启 session 属于 Gateway / 后台控制面职责。

规则：

- 不允许带 `decisionLeaseId`。
- `payload` 只带 `session + stop`。
- `payload.session.state` 必须是 `Stopped` 或 `Failed`。
- `payload.session.controllable` 必须为 `false`。
- `session_stopped` 不携带被中断 skill 摘要；如需排查中断中的 skill，使用 `traceId / sessionId / skillCallId` 查 Gateway 日志。
- 同一个 `sessionId` 下，`session_stopped` 必须是最后一个 Gateway -> LLM 业务事件；之后不再发送 `skill_finished`、`observation_updated` 或任何带 `decisionLeaseId` 的事件。
- 如果后续重新托管、重新登录或重启托管流程，必须生成新的 `sessionId`，并重新发送新的 `session_started`。
- 如果 Gateway 已经成功发送带 `decisionLeaseId` 的事件，但 LLM 决策前发生玩家上线、后台停止、Gateway 关闭等外部终止，Gateway 应让当前 lease 失效，并发送 `session_stopped` 表达终态。
- 外部终止后，LLM 再使用旧 lease 调 `/decision`，Gateway 返回 `status=rejected / reason=lease_expired`。

### `payload.stop`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `reason` | 是 | string | 停止原因机器码。 |

`payload.stop` 只允许 `reason` 一个字段。

| stop.reason | session.state | 说明 |
|---|---|---|
| `admin_stop` | `Stopped` | 后台、人工或管理端停止托管。 |
| `stop_hosting_requested` | `Stopped` | LLM 自己提交 `action=stop_hosting`。 |
| `player_online` | `Stopped` | 玩家本人上线，Gateway 应让出账号。 |
| `server_kicked` | `Stopped` | 游戏服务器踢下线。 |
| `gateway_shutdown` | `Stopped` | Gateway 正常下线、维护或重启。 |
| `runtime_error` | `Failed` | Gateway 内部运行错误导致整个 session 失败。 |

`session_stopped.reason=runtime_error` 表示整个托管 session 因 Gateway 内部错误进入 `Failed` 终态；`skill_finished.reason=runtime_error` 只表示某次 skill 执行失败但 session 仍可能继续。

## `observation_updated`

含义：Gateway 主动给 LLM 一次新的状态观察和下一次决策机会。它是“下一轮决策触发事件”，不是所有状态变化的广播事件。

典型触发：

- LLM 上一次提交 `wait`，Gateway 等待结束后发出下一轮观察。
- Gateway 在 session 空闲且可控时发现关键状态变化。
- Gateway 恢复可控或观察到关键状态变化后，需要让 LLM 基于最新状态继续决策。
- 当前 skill 仍在执行，但该 skill 可打断，且 Gateway 观察到更高优先级状态。

规则：

- 必须带 `decisionLeaseId`。
- `payload` 只带 `session + observation`。
- 不携带 `availableSkills / skillArgumentHints / lastSkillResult`。
- 不携带当前正在执行的 `runningSkill`；如 LLM 后续确实需要查询当前执行状态，归到只读 Query 或技能规则讨论。
- 不携带 `priority / triggerSkillCallId / interruptible` 这类抢占细节；是否允许抢占由 Gateway 内部技能规则判断。
- 不应该按每帧、每次坐标微变、每次活性检查发送；必须按关键变化或等待结束限流触发。
- 如果 Gateway 不能给出新的决策 lease，例如当前 `controllable=false`，就不要发送 `observation_updated`；等到可控时再发，或终态时发 `session_stopped`。
- 如果当前已有 skill 在执行，`observation_updated` 带 `decisionLeaseId` 表示 Gateway 允许 LLM 基于当前状态提交一次新决策；是否能打断旧 skill 仍由 Gateway 按内部 skill 规则判断。
- 如果只是为了表达 skill 结果，不要把 skill 结果塞进 `observation_updated`，应使用 `skill_finished`。

### `payload.observation`

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `reason` | 是 | string | `wait_completed` 或 `state_changed`。 |

| reason | 说明 |
|---|---|
| `wait_completed` | LLM 上一次 `wait` 等待完成。 |
| `state_changed` | Gateway 观察到关键状态变化。 |

## Event Receive Response

LLM 响应只表达是否接收了事件，不直接返回业务决策。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `status` | 是 | string | `accepted` 或 `duplicate`。 |
| `eventId` | 是 | string | 必须等于请求里的 `event.eventId`。 |

示例：

```json
{
  "status": "accepted",
  "eventId": "evt-session-001"
}
```

规则：

- LLM 必须按 `eventId + bodySha256` 去重。
- LLM 收到重复事件时应返回 HTTP 200，`status=duplicate`，`eventId` 原样返回。
- 所有事件都按 `eventId + bodySha256` 去重，包括 `session_stopped`。
- LLM 收到已存在的 `eventId` 但本次 `bodySha256` 与第一次不同时，按协议错误处理，返回 HTTP `400 bad_request`，不返回 `accepted/duplicate`。
- Gateway 收到 `status=duplicate` 应按投递成功处理并停止重试。
- LLM 返回 `status=accepted` 或 `status=duplicate` 时，`eventId` 都必须等于请求里的 `event.eventId`；不匹配时 Gateway 应记录为异常响应。
- LLM 只有在 HMAC、JSON 和字段校验通过，`eventId + bodySha256` 幂等记录已落下，并且事件已进入 LLM 可恢复处理队列或已同步处理完成后，才能返回 `status=accepted`。
- LLM 不能只因 HTTP 请求已到达、但事件仍停留在不可恢复的临时内存中，就返回 `status=accepted`。
- LLM 一旦返回 `status=accepted`，Gateway 即认为事件已经可靠交付，不再重试该事件。
- HTTP 200 但响应 body 不是合法 JSON、缺 `status`、`status` 不是 `accepted/duplicate`、缺 `eventId` 或 `eventId` 不匹配时，Gateway 不当作投递成功；记录异常响应，是否重试由 Gateway 内部策略决定。
- 请求级错误直接返回 HTTP `400/401/429/500`，不返回 `accepted/duplicate`。
- 事件投递失败、重试上限以及 LLM 不可达时的 session 处理规则见 `llm-gateway-auth-idempotency.md`。
