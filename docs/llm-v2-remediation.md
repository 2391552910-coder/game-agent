# myAgent 接入 Gateway v2 改造清单

本文只描述 myAgent 侧必须完成的修改。每项包含原因、修改要求和验收标准。

## LLM-V2-001：实现精确 capabilities

### 原因

Gateway 只会在 provider 明确支持完整 v2 合同时启用 LLM 控制。能力缺失或虚报会使双方使用不兼容的事件、ACK 和 decision 语义。

### 修改要求

- 在 `/capabilities` 提供能力接口。
- `contractVersion` 必须精确为 `llm-gateway-http-v2`。
- `receiveEventsPath` 必须为正式事件入口 `/api/gateway/events`。
- `supportedDecisionActions` 声明 `call_skill`、`wait`、`no_op`、`stop_hosting`。
- `maxEventBatchSize` 和 `maxDecisionTtlMs` 必须为正数。
- `perEventAck`、`controlGeneration`、`eventSequence`、`asyncSkillTerminal` 必须为 `true`。
- `supportedEventTypes` 只声明：`session_started`、`observation_updated`、`skill_started`、`skill_finished`、`decision_rejected`、`session_stopped`。
- 只有完成本文全部合同后才能声明 v2；不得把缺字段、404 或不可达当成兼容成功，也不得自动回退到其它版本。

### 验收

- 完整 v2 capabilities 可被 Gateway 接受。
- 版本错误、字段缺失、无效上限和虚假能力均被 Gateway 拒绝。

## LLM-V2-002：ACK 前持久接纳完整事件

### 原因

Gateway 收到 ACK 后会认为该事件已经由 myAgent 接管。只保存 `eventId -> bodyHash` 或只启动进程内后台任务，无法在业务失败或进程退出后恢复事件。

### 修改要求

- 返回 received ACK 前，原子保存规范化后的完整事件或等价可重放表示。
- 同时保存 `eventId`、内容 hash、`gatewayId`、`sessionId`、`controlGeneration`、`eventSequence` 和处理状态。
- 处理状态至少区分待处理、处理中、成功、可重试失败和人工处理状态。
- event hash 只覆盖稳定 gateway identity 和不可变事件内容；排除 requestId、batch trace、`sentAtMs`、签名头和其它 transport 元数据。
- 新 eventId 持久接纳成功后加入 `receivedEventIds`。
- 相同 eventId、相同内容加入 `duplicateEventIds`，不得重复启动业务。
- 相同 eventId、不同内容返回非 2xx 协议冲突。
- 持久化失败的事件不得列入 received 或 duplicate。
- 整批 schema、HMAC 和 eventId 内容冲突必须在写入前完成 preflight；部分持久接纳成功时，只 ACK 成功项，其余项留给 Gateway 重试。

ACK 结构固定为：

```json
{
  "accepted": true,
  "traceId": "...",
  "receivedEventIds": ["..."],
  "duplicateEventIds": []
}
```

### 验收

- worker 失败或进程重启后，已 ACK 事件仍可继续处理。
- 同一事件更换 requestId 或 batch trace 后重投仍识别为 duplicate。
- eventId 内容冲突、部分持久化失败和整批 preflight 失败均符合上述 ACK 规则。

## LLM-V2-003：按 controlGeneration 和 eventSequence 顺序消费

### 原因

HTTP 重试、批量投递和多 session 并发会改变到达顺序。`stateVersion` 只描述决策状态，不能替代事件序号；旧控制周期也不能覆盖新控制周期。

### 修改要求

- 以 `gatewayId + sessionId + controlGeneration` 作为独立消费分区。
- 同一分区内按 `eventSequence` 严格递增处理，sequence 从 1 开始。
- sequence 出现 gap 时等待缺失事件，不得越过处理后续事件。
- duplicate 只合并状态，不重复执行业务。
- `session_started(sequence=1)` 建立控制周期。
- `session_stopped` 只关闭对应 controlGeneration，不影响同 session 的新 controlGeneration。
- 新 controlGeneration 建立后，旧 controlGeneration 的事件不得覆盖当前 Agent 状态或产生新 decision。
- lease 过期或状态过旧时，等待新的 lease-bearing event；不得更换 decisionId 盲目重发旧计划。
- myAgent 重启后可以恢复 durable inbox，但不得使用已失效的旧 lease 主动重放 decision。

### 验收

- 两个 session 可以并行处理。
- 同 session 同 controlGeneration 不越过 gap。
- 旧 controlGeneration 的迟到事件和 decision 不污染新 controlGeneration。

## LLM-V2-004：按 eventType 解析最小 payload

### 原因

不同事件承担不同职责。进度、拒绝和关闭事件不需要决策上下文，也不应被错误地当成可发起下一次决策的授权。

### 修改要求

| eventType               | decision lease | 处理要求                                                                          |
| ----------------------- | -------------- | --------------------------------------------------------------------------------- |
| `session_started`     | 有             | 建立 controlGeneration，保存决策上下文，允许触发 Agent。                          |
| `observation_updated` | 有             | 更新决策上下文，允许触发 Agent。                                                  |
| `skill_started`       | 无             | 按`decisionId + skillCallId` 标记开始，不触发新决策。                           |
| `skill_finished`      | 可有           | 收敛唯一终态；有 lease 时更新上下文并允许下一决策，无 lease 时只更新执行状态。    |
| `decision_rejected`   | 无             | 与 HTTP rejected 按 decisionId 合并，不触发新决策，等待后续 lease-bearing event。 |
| `session_stopped`     | 无             | 关闭对应 controlGeneration，取消尚未发送的计划。                                  |

- 使用 eventType 判别 schema，不得要求六类事件携带同一 payload。
- 只有 lease-bearing event 才要求安全 session snapshot、`availableSkills`、`skillArgumentHints`、技能结果和 lease 信息。
- 认证、schema、session、lease missing/expired/consumed、controlGeneration 或 stateVersion 不匹配可能只返回 HTTP rejected，不保证存在 `decision_rejected` 事件。
- 收到 HTTP-only stale rejection 时，不得推断 Gateway 已经发放新 lease。

### 验收

- 六类事件分别通过合法与非法 payload 测试。
- 无 lease 事件不会触发 Agent 或产生新 decision。
- `decision_rejected` 只有在后续收到新的 lease-bearing event 后才允许继续决策。

## LLM-V2-005：把 Gateway 决策上下文交给 Agent

### 原因

如果 Agent 只接收 session snapshot，而丢弃 Gateway 发布的能力、参数提示、lease 范围和上次结果，模型会生成未授权 skill、错误参数或与当前执行状态冲突的动作。

### 修改要求

- 对每个 lease-bearing event，把以下信息传入 Agent：
  - 安全 session snapshot；
  - `availableSkills` 及每项 `schemaVersion`；
  - `skillArgumentHints` 和允许字段；
  - lease kind 及其动作范围；
  - 本事件携带的技能终态。
- Agent 最终输出必须是当前 `availableSkills` 与 lease 允许范围的子集，不能使用固定技能列表扩大能力面。
- `movement_control` 下只允许能力交集中的 `jump`、`stop_move`，以及 lease 允许的 `wait`、`no_op`、`stop_hosting` actions。
- 不得把 Gateway 内部 policy 中的 `ground` 当成已发布 LLM skill。
- `play_action.arguments` 使用 `actionId`，不得使用 `action`。

### 验收

- 不同 `availableSkills` 输入会实际收窄 Agent 可输出能力。
- movement control 不会产生 `ground` 或其它未发布 skill。
- `play_action.actionId` 的模型、prompt、序列化和测试全部一致。

## LLM-V2-006：稳定提交 decision 并快速结束 HTTP

### 原因

skill 在 Gateway 内异步执行，HTTP accepted 只表示 Gateway 已接管。HTTP response 与 `skill_started/skill_finished` 可能乱序，传输重试也不能创建第二次执行。

### 修改要求

- decision request 必须携带并原样保持 `controlGeneration`。
- 每次计划动作在首次发送前生成稳定 `decisionId`。
- HTTP timeout 或可重试传输失败时，使用相同 decisionId 和完全相同 body 重试。
- 相同 decisionId 不得对应不同 body；发生冲突时停止自动重试并记录调用方错误。
- `call_skill` 和 `stop_hosting` 收到 accepted 后，以响应中的稳定 `skillCallId` 建立 pending call，不等待 HTTP 返回技能终态。
- 使用短 transport timeout；不得用长 HTTP timeout 等待长技能完成。
- callback client 必须先读取并校验 response body，再按 HTTP status 分类。
- 合法 `status=rejected` 即使使用非 2xx 也返回结构化拒绝；非 JSON、非法字段组合，或非 2xx 且不是合法 rejected 时，才归为 transport/contract failure。
- `reason` 按非空字符串处理；未知 reason 仍作为结构化 rejected，不得因本地硬编码 allowlist 转成网络故障。
- HTTP accepted/rejected 不读取或生成下一 lease。下一次决策只由 `session_started`、`observation_updated` 或带 lease 的 `skill_finished` 授权。
- 先收到 skill 事件时，按相同 `decisionId + skillCallId` 建立或更新调用；迟到 accepted 只合并，不重复启动。
- 相同 decisionId/body 的幂等 response 只表示原请求结果，不表示新的执行授权。

### 验收

- HTTP accepted 先到和 skill 事件先到两种顺序都只形成一个逻辑调用。
- 相同 body 重试复用同一 skillCallId；不同 body 返回幂等冲突。
- 合法非 2xx rejected 被解析为结构化结果，非法响应才归为传输或合同错误。
- accepted、rejected、wait 和 no-op response 都不会产生第二条 lease 来源。

## LLM-V2-007：按唯一终态和结构化错误推进状态

### 原因

HTTP accepted 不是技能成功。重复事件、迟到结果和不同失败类型如果没有统一收敛，会重复推进目标或发起不安全重试。

### 修改要求

- 每个 `skillCallId` 只接受一个逻辑 `skill_finished`；duplicate eventId 或重复终态只做幂等合并。
- `success` 不带 `failureCategory`。
- `failed` 的 `failureCategory` 只接受 `business_rejected`、`transport_failed`、`protocol_failed`、`internal_failed`。
- `cancelled` 和 `timeout` 不带 `failureCategory`，根据稳定 `reason` 和 `retryable` 决定后续动作。
- `vehicle_completion_unconfirmed` 和 `completion_unconfirmed` 不得自动重试原动作。
- 不解析 Gateway 内部自由文本、Java 原始 Message 或上游错误码猜测分类。

### 验收

- success、四类 failed、cancelled 和 timeout 均有独立测试。
- 重复和迟到 terminal 不会二次推进目标。
- 不可确认完成状态不会触发原动作重试。

## LLM-V2-008：ACK 后的内部失败由 myAgent 自己恢复

### 原因

事件一旦 durable ACK，Gateway 不会依靠 transport 重投来恢复 myAgent 内部业务失败。只记录异常并结束会永久丢失该事件对应的处理责任。

### 修改要求

- Agent、数据库、模型调用和 decision callback 失败必须进入有界重试或可观测 dead-letter 状态。
- 重试复用原始事件、controlGeneration、eventSequence 和 decisionId，不创建新的逻辑动作。
- 不得删除 eventId claim 后要求 Gateway 重新投递。
- 人工处理状态只保存脱敏错误分类、阶段和关联 ID，不保存完整模型输入、凭证或原始协议对象。

### 验收

- 注入 Agent、数据库和 callback 失败后，事件可重试或进入 dead-letter，不会静默完成。
- 重试不会生成第二个 decision 或重复执行已接管的 skill。

## LLM-V2-009：分离双向身份并严格解析 tenant

### 原因

Gateway -> myAgent 事件和 myAgent -> Gateway decision 是两个相反的信任方向。共用身份会扩大凭证权限；把任意 gatewayId 当 UUID tenant 会把配置错误延迟成数据库异常。

### 修改要求

- Gateway -> myAgent 入站认证使用 `LLM_GATEWAY_APP_SECRETS` 和 `LLM_GATEWAY_APP_TENANTS`。
- myAgent -> Gateway decision 使用 `LLM_GATEWAY_DECISION_URL`、`LLM_GATEWAY_DECISION_APP_ID` 和 `LLM_GATEWAY_DECISION_APP_SECRET`。
- 两组身份不可互换或隐式复用。
- gatewayId 到 UUID tenant 必须显式映射。
- 缺少映射或 tenant 不是合法 UUID 时，在进入 Agent 和数据库前 fail-closed，返回稳定、脱敏且可诊断的结果。
- 配置只保存变量形状或 secret-file 引用；真实凭证、tenant 值和本机路径不得进入代码、日志或测试夹具。

### 验收

- 两个方向使用各自正确身份时认证成功，交换身份时稳定返回 401。
- tenant 缺失或格式错误不会进入数据库查询。
- 日志和错误响应不包含凭证或 tenant 配置原文。

## LLM-V2-010：收紧运行日志

### 原因

依赖初始化、SDK debug、SQL echo 和异常全文可能输出认证 URI、完整模型输入、数据库参数和其它敏感内容。

### 修改要求

- 在依赖初始化前设置敏感依赖 logger 级别。
- 关闭 SQL echo 和会打印 SQL 参数的普通日志。
- Agent、数据库和模型异常只记录稳定阶段、异常类型、脱敏关联 ID 和必要时长。
- 普通日志不得包含认证 URI、password/token、完整 prompt、完整 model request/response、session snapshot、SQL/参数或 Java 原始对象。

### 验收

- 启动、事件处理、模型失败、数据库失败和 callback 失败场景的日志扫描均不包含上述敏感数据。
- 仍能通过 traceId、eventId、decisionId、skillCallId、阶段和错误分类定位问题。

## LLM-V2-011：保证外部依赖 readiness

### 原因

Embedding 或 Rerank 不可用会使事件业务在生成 decision 前失败，并持续占用重试与处理资源。

### 修改要求

- readiness 必须覆盖实际启用的 Embedding 和 Rerank 依赖。
- 依赖被配置为禁用时，事件处理路径不得继续调用该依赖。
- 运行中依赖失败必须进入 `LLM-V2-008` 的重试或 dead-letter 流程，不得把事件标记成功。
- 不得用 mock 依赖作为生产 readiness 结果。

### 验收

- 启用且可用时 readiness 通过，事件可形成 decision。
- 启用但不可用时 readiness 失败或事件进入可恢复失败状态。
- 显式禁用时不会发出对应网络请求。

## LLM-V2-012：提供无秘密测试配置并清理静态检查

### 原因

纯 mock unit/API tests 如果依赖开发机私有配置，就无法在新环境和 CI 中稳定运行；静态检查债务也会掩盖新改动引入的问题。

### 修改要求

- 在测试设置加载的最早边界提供明确的非秘密占位配置。
- 占位配置只满足类型校验，不指向真实服务，也不回退读取开发机 `.env`。
- 生产启动缺少必填配置时仍然 fail-closed。
- 修复 `tests/unit/test_decision_nodes.py` 和 `tests/unit/test_nodes.py` 的 Ruff 问题。
- 将本次修改的生产文件、测试文件以及上述两个测试文件纳入 CI 静态检查范围。

### 验收

- 新 checkout 无私有 `.env` 也能运行非网络 unit/API tests。
- 测试进程不会向占位 URL 发真实请求。
- 生产配置缺失测试仍能证明启动失败。
- 目标文件 Ruff 检查通过。

## 完成标准

- `LLM-V2-001` 至 `LLM-V2-012` 的验收项全部有自动化证据。
- Gateway v2 capabilities、events、ACK 和 decision contract tests 全部通过。
- 单事件、混合 batch、duplicate、内容冲突、partial ACK、顺序 gap、旧 controlGeneration、HTTP/event 乱序和进程恢复均有覆盖。
- Agent 输出受 `availableSkills` 和 lease kind 收窄，`play_action` 使用 `actionId`。
- 双向身份、tenant fail-closed、日志安全、外部依赖 readiness 和无秘密测试配置全部通过。
