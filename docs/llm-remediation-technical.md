# LLM 侧整改技术说明

## 范围

本文只列 MyAgent / LLM owner 需要修改的代码、原因和验收标准。聊天 API、聊天事件、聊天工具、聊天 prompt 与聊天测试不在本文范围内。

## 状态总览

| ID | 优先级 | 必须动作 |
| --- | --- | --- |
| `LLM-V2-001` | P1 | 让 v2 事件 schema 与 Gateway 正式 envelope 完全等形 |
| `LLM-V2-002` | P1 | 在 Agent context 透传 lease 父技能元数据 |
| `LLM-V2-003` | P1 | 让 skill terminal 按 skillCallId 独立收敛，不被新 cycle 丢弃 |
| `LLM-V2-004` | P1 | 解耦事件 ACK、Agent 决策和 capabilities 可用性 |
| `LLM-V2-005` | P2 | 使用 Gateway 导出的六类非聊天 fixture 做合同测试 |
| `LLM-V2-006` | P1 | 使 v2 本地/部署配置满足 Gateway allowlist、tenant 与 readiness 合同 |
| `LLM-SEC-001` | P1 | 按通信方向隔离两组 HMAC 身份 |

## LLM-V2-001：事件 schema 必须与 Gateway 正式合同等形

### 原因

MyAgent 不能继续使用手写的旧事件形状。已用 Gateway 实际事件 JSON 在通过 HMAC 后直接复现 Pydantic `ValidationError`，因此该问题不是签名、网络或 Gateway 回调失败：验签失败应为 `401`，验签成功但模型不兼容才是当前的 `400`。

当前合同差异如下：

| Gateway 正式事件 | MyAgent 当前错误假设 |
| --- | --- |
| 根部有 `stateVersion`、`decisionLeaseId` | 未声明且 `extra=forbid` 拒绝 |
| lease payload 为 `reason + lease + decisionContext` | 将 `session/availableSkills/skillArgumentHints` 误放入 `lease`，并要求不存在的 `allowedDecisionActions` |
| `payload.lease` 含 `sessionId/controlGeneration/allowedSkillName/parentSkillName/allowedActions/allowedSkillNames` | 把上述 Gateway 合法字段视为额外字段 |
| `skill_finished.payload` 是扁平终态 | 错误要求嵌套 `terminal` 对象 |
| `availableSkills` 为 PascalCase 完整 `SkillDescriptor` | 仅接受两个 camelCase 字段 |
| `allowedArgs/missingArgs` 为对象数组 | 错误定义为字符串数组 |

保留 `extra=forbid` 是正确的，但严格校验必须以 Gateway 正式合同为基准，不能靠删除或忽略合法字段来通过解析。

### 修改要求

- `GatewayEventEnvelope` 接收根字段 `stateVersion`、`decisionLeaseId`。
- lease 接收 `sessionId`、`controlGeneration`、`stateVersion`、`decisionLeaseId`、`leaseKind`、`allowedActions`、`allowedSkillName(s)`、`parentSkillName`。
- `session_started`、`observation_updated`、`skill_started`、`skill_finished`、`decision_rejected`、`session_stopped` 分别使用独立 payload model。
- `skill_finished` 使用扁平 `skillName/skillCallId/status/reason/failureCategory/retryable/startedAtMs/finishedAtMs`，不要要求不存在的 `terminal` 包装。
- `skill_finished.status` 只接受 `success/failed/cancelled/timeout`；`rejected` 只属于独立的 `decision_rejected` 事件，必须在终态 schema 中拒绝。
- `availableSkills` 接收 Gateway 完整 `SkillDescriptor`；`allowedArgs/missingArgs` 接收对象数组，不得降成字符串数组。
- 保持 `extra=forbid`，但禁止通过删除 Gateway 合法字段来获得严格校验。

### 涉及文件

- `src/core/integration/llm_gateway_v2/contracts.py`
- `src/core/integration/llm_gateway_v2/event_service.py`
- `src/core/integration/llm_gateway_v2/decision_service.py`
- 对应六类事件和 decision contract tests

### 验收

- 六类 Gateway fixture 均通过 Pydantic 校验。
- 缺根字段、错误 lease 层级、错误终态层级和未知额外字段仍稳定失败。
- `skill_finished.status=rejected` 稳定校验失败，不能与 `decision_rejected` 混为同一种事件。
- 所有聊天 fixture 和聊天路由保持未接入。

## LLM-V2-002：Agent context 必须透传父技能元数据

### 原因

`movement_control`、`vehicle_cancel_window` 和 `vehicle_recovery` 都需要知道当前 lease 对应的父技能。当前 selector 能看到 `leaseKind`，但拿不到 `payload.lease.parentSkillName`，只能依赖不可靠的 session 快照；移动窗口内 `session.ExecutingSkillName` 可能为空。

### 修改要求

- 在 `GatewayV2AgentContext` 增加只读 `parent_skill_name`。
- 同时透传 `allowed_skill_name`、`allowed_skill_names`，避免 Agent 再从 session 文本推断。
- context builder 只从已校验的 `payload.lease` 取值，不复制完整原始 payload。
- 决策前校验目标 skill 属于 lease 允许集合。

### 验收

- `move_to -> stop_move` 能证明父技能为 `move_to`。
- 两种载具窗口分别只允许自己的配对 exit。
- session 快照缺少 executing skill 时仍能正确关联父动作。

## LLM-V2-003：终态必须按 skillCallId 独立收敛

### 原因

新 control cycle、重复 `session_started` 或 generation 切换不能抹掉已 accepted 调用的事实终态。当前实现会把合法 `skill_finished` 标成 `superseded`、`dead_letter` 或长期 `pending`，造成：

- 配对 `stop_move` 成功后，父 `move_to` 仍停在 `started`。
- 热气球 exit 的 `success/ok` 被标成 `superseded`。
- 直升机 auto terminal 进入 `dead_letter`，exit terminal 长期 `pending`。

### 修改要求

- 以 `(gatewayId, skillCallId)` 作为 terminal 幂等键。
- terminal repository 对已有 accepted/started call 执行单调状态转移；重复 terminal 幂等成功。
- current cycle / generation 只限制新 decision 和副作用，不能阻止旧调用的 immutable terminal 落库。
- 对 terminal 的 `decisionId`、`skillName`、`sessionId` 做一致性校验；不匹配进入可诊断失败，不覆盖其它调用。
- `superseded` 只用于尚未执行的旧 observation/session event，不用于已 accepted skill 的最终事实。

### 涉及文件

- `src/core/integration/llm_gateway_v2/terminal_repository.py`
- `src/core/integration/llm_gateway_v2/outbox_repository.py`
- `src/core/integration/llm_gateway_v2/event_service.py`
- `src/core/integration/llm_gateway_v2/event_worker.py`
- `src/core/integration/llm_gateway_v2/inbox_repository.py`
- terminal state / event worker / recovery integration tests

### Generation 回收边界

根因位于 terminal repository 之前：`classify_generation` 会把旧 generation 的所有事件归为 `STALE`，`InboxRepository` 随即把事件写为 `superseded`，使 `TerminalRepository.record_skill_finished` 永远不会执行。载具 exit 和父 `move_to` 长期停在 `started` 都符合这条失败路径。

实现必须遵守以下边界：

- 旧 generation 的 `session_started`、`observation_updated` 和 `session_stopped` 继续禁止重放或影响当前 runtime。
- 仅允许 `skill_started`、`skill_finished`、`decision_rejected` 进入历史回收路径；它们只能按原 `decisionId` / `skillCallId` 更新旧记录，不能持久化 lease、调用 Agent、创建 decision 或重新激活旧 cycle。
- claim 续租、完成和过期扫描也必须识别该历史回收路径，不能在 worker 完成前再次改写为 `superseded`。
- `tests/integration/test_gateway_v2_recovery.py` 的事件工厂必须持续使用正式 v2 envelope；此前工厂仍使用旧嵌套 lease/terminal 形状，导致集成测试在业务断言前就因缺少 `stateVersion`、`decisionLeaseId`、`decisionContext` 或 `stoppedAtMs` 失败。

数据库回归必须证明：generation 2 已 active 后，generation 1 的 `skill_finished` 仍可收敛为 `succeeded`，同时 generation 2 保持 active，且不会产生新的 decision 或 Agent 副作用。

### 验收

- parent move cancelled、vehicle auto timeout/cancelled、vehicle exit success 均在对应 skill row 收敛。
- terminal 先于 `skill_started`、晚于新 cycle、重复到达三种顺序均有测试。
- generation 2 active 后，generation 1 的 `skill_started`、`skill_finished`、`decision_rejected` 分别可收敛到原记录，且不创建新 decision、不运行 Agent、不改变 generation 2。
- 进程重启后 pending terminal 可以继续处理，且不会重复副作用。

## LLM-V2-004：事件 ACK 与 Agent 决策必须解耦

### 原因

Agent 没有立即产出 action、测试计划耗尽或推理超时时，不应让 `/api/gateway/v2/events` 返回 400，也不应让 `/api/gateway/v2/capabilities` 变成 503。否则 Gateway 会重复投递、cycle 堆积，并进一步触发 terminal supersede/dead-letter。

### 修改要求

- webhook 完成 HMAC、schema、幂等和持久化后快速 ACK。
- Agent 选择和 decision callback 由有界 worker/outbox 异步执行。
- Agent 暂无动作时产生合法 `wait`/`no_op`，或保持 pending 并重试；不得用 400 表示“当前没有计划”。
- capabilities 只反映静态协议兼容和服务 readiness，不依赖某次 Agent 计划是否耗尽。
- worker 失败必须有退避、最大重试和 dead-letter 诊断，不阻塞同 session 的 terminal ingestion。

### 验收

- Agent 超时、返回空动作、抛异常时 event ACK 和 capabilities 仍符合合同。
- 同一 session 的 terminal 可在 decision worker 失败时继续落库。
- 重复事件不会重复创建 decision 或 skill call。

## LLM-V2-005：fixture 必须来自 Gateway 真源

### 修改要求

- 固化六类非聊天事件 fixture，并记录生成它们的 Gateway 合同版本。
- 覆盖 PascalCase `SkillDescriptor`、对象型参数提示、四种 lease kind 和扁平 terminal。
- 增加 contract drift 测试：Gateway fixture 变化时 MyAgent CI 必须失败。
- 测试集合显式排除聊天，不用聊天 fixture 证明非聊天协议通过。

## LLM-V2-006：v2 配置与 readiness 必须可重复启动

### 原因

v2 启动合同除 `LLM_GATEWAY_APP_SECRETS` 和 tenant 映射外，还强制要求 `LLM_GATEWAY_APP_GATEWAYS`。GatewayId 对应 tenant 必须是数据库中已存在的 UUID，普通名称或 GatewayId 本身不能代替 tenant UUID。运行数据库 revision 必须达到 v2 所需的 `009`；启用但不可达的 Rerank 也会让 capabilities fail-closed 为 `503`。

### 修改要求

- 部署配置必须声明 `LLM_GATEWAY_APP_GATEWAYS={AppId:[GatewayId...]}`，且每个 v2 AppId 在 `LLM_GATEWAY_APP_SECRETS` 中有入站 secret。
- `LLM_GATEWAY_APP_TENANTS` 必须为每个 allowlisted GatewayId 指向已存在的 UUID tenant；禁止使用名称或 GatewayId 代替 tenant UUID。
- 运行数据库必须先升级到代码 Alembic head；当前 v2 inbox/outbox head 为 `009`。
- Rerank/embedding 等被 readiness 纳入的依赖必须真实可达，或在该环境显式关闭；不能以 capabilities `503` 运行 Gateway v2。
- secret 继续只放部署环境或 secret-file 引用，本文和 fixture 不保存值。

### 验收

- `/ready` 的 database、eventWorker、decisionWorker 为 ready；可选依赖要么 ready，要么 disabled/skipped。
- `/api/gateway/v2/capabilities` 返回 `200`，并声明精确 `llm-gateway-http-v2` 与 `/api/gateway/v2/events`。
- 缺少 allowlist、tenant 非 UUID、数据库 revision 不匹配、启用依赖不可达分别有 fail-closed 覆盖。

## LLM-SEC-001：按通信方向隔离 HMAC 身份

### 修改要求

- Gateway -> MyAgent 入站：使用 provider 入站身份映射。
- MyAgent -> Gateway decision/query：使用 Gateway 入站身份。
- 两组 AppId/AppSecret 不复用，不写入仓库、fixture 或日志。
- 两组身份互换或混用时必须稳定返回 401。

## 最终验收命令

```powershell
uv run pytest tests/unit/llm_gateway_v2 tests/api/test_gateway_v2.py tests/api/test_gateway_v2_lifespan.py -q -k "not chat"
uv run pytest tests/unit tests/api -q -k "not chat"
uvx ruff check <实际修改的 Python 文件>
git diff --check
```

验收还必须包含真实非聊天事件回放：六类事件全部 ACK，21 个 skill decision 能构造，parent/terminal/cycle 场景收敛，聊天调用数为 0。
