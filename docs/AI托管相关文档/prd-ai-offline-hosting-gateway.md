# PRD: AI 离线托管中转层规划

## 状态
- 日期：2026-04-16
- 阶段：Ralplan 共识规划草案
- 输入规格：`.omx/specs/deep-interview-ai-offline-hosting.md`
- 实现边界：独立项目，技术栈中立，不修改当前 Unity 客户端工程

## 需求摘要
设计一套独立的 AI 托管中转层，使服务端内网大模型 Agent 能通过受控 skill 接管角色基础行为。

`v0.1` 目标是单账号登录并在指定场景稳定完成移动、跑、跳、基础动作闭环。`v1.0` 目标是多个 AI 托管账号并发运行一段时间，且不掉线、不乱发协议。

当前阶段只提供架构思路、模块职责、接口边界、数据流和验收标准。后续实现可使用 Python 或其他服务端技术栈，不绑定 Unity、ET、C# 或当前客户端仓库结构。

## RALPLAN-DR 摘要
### 原则
- **独立边界优先**：AI 调度、中转层、会话池、skill 执行器必须位于独立项目。
- **技术栈中立**：方案不规定语言、框架、部署方式或仓库结构。
- **大模型只决策**：大模型只调用受控 skill，不直接登录、不拼协议、不持有敏感凭证。
- **中转层强约束**：所有动作必须经过 schema、权限、状态、限频、审计和熔断。
- **版本化扩展**：`v0.1` 功能少，但能力注册表、协议适配器和审计模型要支持后续扩展。
- **协议契约单一来源**：独立项目只能消费明确版本的协议契约产物，不能各自猜测或运行时耦合客户端工程。

### 决策驱动
- **不改客户端**：当前客户端只能作为只读协议与行为语义参考。
- **先证明闭环**：`v0.1` 必须可观测地完成登录、进场景、移动、跑、跳、动作。
- **防止扩展返工**：架构要支持未来逐步覆盖更多客户端可操作行为。
- **交付可验收**：即使具体数值后续由业务方填写，计划也必须定义指标字段和证据格式。

### 可选方案
#### 方案 A：大模型直接模拟客户端协议
- 优点：链路最短，首个 demo 可能最快。
- 缺点：大模型容易幻觉参数，协议耦合严重，权限和审计难做，后续并发治理成本高。
- 结论：不采用。它违背“大模型只决策”和“中转层强约束”原则。

#### 方案 B：中转层 + skill/tool + 协议适配器
- 优点：大模型只输出高层动作，中转层统一治理登录、状态、权限、限频、协议适配和审计。
- 缺点：`v0.1` 初始工程量比裸发协议略高，需要先定义能力模型和执行框架。
- 结论：采用。它最符合独立项目、技术栈中立和长期扩展目标。

#### 方案 C：服务端直接写传统机器人逻辑
- 优点：确定性强，易做业务规则控制。
- 缺点：偏离“大模型 AI 决策角色行为”的目标，难体现真实 AI 代理价值。
- 结论：不作为主方案。可在后续高风险能力中作为兜底规则层参考。

## ADR
### Decision
采用 **中转层 + skill/tool + 协议适配器** 的独立项目方案。

### Drivers
- 需要让大模型接管基础客户端行为，但不能修改当前客户端工程。
- 需要让后续实现同事可用 Python 或其他技术栈落地。
- 需要避免大模型直接发裸协议导致幻觉、越权、乱发和审计困难。

### Alternatives Considered
- 大模型直接裸发协议：被拒绝，风险过高且难扩展。
- 服务端传统机器人：被拒绝，偏离大模型托管目标。
- 当前客户端内嵌 AI 调度：被拒绝，违反独立项目边界。

### Why Chosen
中转层方案把“思考”和“执行”分离。大模型负责选择意图，中转层负责把意图变成受控动作，并在发送真实协议前完成校验、限频和审计。

同时把“大模型 Agent”抽象为 `DecisionProvider`。`DecisionProvider` 可以是大模型、脚本、规则引擎或回放器。这样 `v0.1 / v1.0` 可以先验证网关、协议适配、审计和并发治理底座，再接入真实大模型。

### Consequences
- `v0.1` 需要先定义能力注册表、skill schema、会话模型、审计模型和协议适配边界。
- 当前 Unity 客户端不需要改动，但独立项目需要能读取或复用协议定义。
- 后续新增能力应新增 skill 和适配器，不应绕过中转层。

### Follow-ups
- 确认托管授权方式是否使用专用 hosting token。
- 确认 `v0.1` 指定场景、测试账号、目标动作白名单。
- 确认 `v1.0` 并发账号数、持续运行时长和稳定性指标。
- 确认协议契约 source of truth、产物发布方式和兼容性验证负责人。
- 确认独立项目仓库位置、部署网络和游戏服接入方式。

## 只读参考证据
这些路径只用于理解现有协议和行为语义，不是实现落点：

- `Assets/Scripts/Codes/Hotfix/Client/Demo/Scene/SceneSync/SceneMsgHelper.cs:12` 参考进场景接口语义。
- `Assets/Scripts/Codes/Hotfix/Client/Demo/Scene/SceneSync/SceneMsgHelper.cs:52` 参考离场景接口语义。
- `Assets/Scripts/Codes/Hotfix/Client/Demo/Scene/SceneSync/SceneMsgHelper.cs:145` 参考角色命令发送入口。
- `Assets/Scripts/Codes/HotfixView/Client/Demo/Role/Controller/UpLoad/RoleUploadSyncHelper.cs:14` 参考角色动作上行语义。
- `Assets/Scripts/Codes/ModelView/Client/Demo/Role/Controller/UploadSync/RoleUploadSyncActionType.cs:5` 参考现有动作枚举。
- `Assets/Scripts/Codes/ModelView/Client/Demo/Role/Controller/UploadSync/RoleUploadSyncActionType.cs:7` 参考移动动作。
- `Assets/Scripts/Codes/ModelView/Client/Demo/Role/Controller/UploadSync/RoleUploadSyncActionType.cs:12` 参考跳跃开始动作。
- `Assets/Scripts/Codes/ModelView/Client/Demo/Role/Controller/UploadSync/RoleUploadSyncActionType.cs:13` 参考跳跃结束动作。
- `Assets/Scripts/Codes/HotfixView/Client/Demo/Role/Controller/UpLoad/RoleUploadMergeQueueComponentSystem.cs:57` 参考当前客户端移动限频思路。
- `Assets/Scripts/Codes/HotfixView/Client/Demo/Role/Controller/CharacterMoveComponentSystem.cs:75` 参考移动行为语义。
- `Assets/Scripts/Codes/HotfixView/Client/Demo/Role/Controller/CharacterMoveComponentSystem.cs:117` 参考跑步行为语义。
- `Assets/Scripts/Codes/HotfixView/Client/Demo/Role/Controller/CharacterMoveComponentSystem.cs:154` 参考跳跃行为语义。
- `Assets/Scripts/Codes/Model/Generate/Server/Message/ET/SgMessage_C_1001.cs:40` 参考业务登录消息。
- `Assets/Scripts/Codes/Model/Generate/Server/Message/ET/SgMessage_C_1001.cs:1329` 参考角色命令请求消息。
- `Assets/Scripts/Codes/Model/Generate/Server/Message/ET_M/OuterMessage_C_10001.cs:321` 参考网关登录消息。

## 目标版本
### v0.1
目标是单账号闭环，不追求并发规模。

必须支持：
- 建立一个托管会话。
- 登录并进入指定场景。
- 获取角色当前状态摘要。
- 执行移动、跑、跳、基础动作。
- 拒绝非白名单动作。
- 记录完整审计链路。

### v1.0
目标是多账号稳定运行和协议治理。

必须支持：
- 多个托管会话并发运行。
- 每个会话独立维护心跳、重连、状态、限频和错误计数。
- 单账号异常隔离，不影响其他账号。
- 连续失败、重复异常动作、非法状态动作触发熔断。
- 按账号回放审计日志。

## 非目标
`v0.1` 和 `v1.0` 暂不支持：

- 聊天和自由文本社交。
- 任务、活动、收益领取。
- 背包、交易、付费、抽奖。
- 战斗、竞技、排行榜。
- 大模型直接拼裸协议字段。
- 修改当前 Unity 客户端。
- 绑定 Python、C#、Go、Java 或任何具体技术栈。
- 运行时直接引用 Unity 客户端仓库、Unity 运行时程序集、FUI 或客户端本地组件。

## 协议契约边界
独立项目与现有游戏协议之间必须存在一层明确的 **Protocol Contract Layer**。

### Source Of Truth
协议 source of truth 应由业务方或服务端协议负责人确认。规划阶段不固定它是 IDL、proto、Luban 配置、生成代码或其他格式。

### 允许消费的产物
独立项目允许消费明确版本的协议契约产物，例如：
- 语言无关 IDL 或 schema。
- 从 source of truth 生成的 DTO / SDK / package。
- 可独立运行的 opcode、序列化和消息定义包。
- 经版本标记的协议映射文档。

### 禁止的耦合方式
- 禁止运行时直接引用当前 Unity 客户端仓库。
- 禁止依赖 Unity Runtime、FUI、客户端场景对象或热更程序集。
- 禁止实现方各自手抄客户端生成代码作为长期方案。
- 禁止大模型直接接触原始协议字段。

### 版本同步
每次协议升级都应至少提供：
- 协议 owner。
- 同步 owner。
- 验收 owner。
- 协议契约版本号。
- 变更摘要。
- 兼容性影响说明。
- 独立项目协议契约一致性测试结果。

如果暂时没有统一 source of truth，`v0.1` 前置交付物应先补一份“协议契约清单”，列出登录、进场景、移动、跑、跳、基础动作所需消息、字段、方向、错误码和负责人。

### 责任人占位表
| 职责 | Owner | 说明 |
| --- | --- | --- |
| 协议 owner | TBD | 确认协议 source of truth 和协议变更影响 |
| 同步 owner | TBD | 负责把协议契约产物同步到独立项目 |
| 验收 owner | TBD | 确认协议一致性测试和业务验收结果 |
| 业务 owner | TBD | 确认 v0.1 指定场景、动作白名单和 v1.0 指标数值 |

## 概念架构
```text
Game Server / State Source
    -> StateObserver
    -> DecisionProvider
    -> SkillRegistry
    -> SkillExecutor
    -> HostingGateway
    -> ProtocolContractLayer
    -> ProtocolAdapter
    -> Game Server
    -> AuditLogger / CircuitBreaker
```

## 模块职责
### DecisionProvider
提供下一步决策，可以是大模型、规则脚本、测试回放器或人工调试器。它只能调用已注册 skill，不能跳过 SkillExecutor。

### HostingSession
维护托管账号的登录态、连接态、当前角色、当前场景、心跳、重连和错误计数。

### StateObserver
把游戏服或托管会话状态转换成大模型可读摘要。摘要应只包含决策需要的信息，不暴露无关内部数据。

### SkillRegistry
注册每个 skill 的名称、版本、入参 schema、风险等级、权限、前置状态、限频策略和执行器。

### SkillExecutor
统一执行 skill。执行前做 schema 校验、权限校验、状态校验、限频校验；执行后记录结果并更新审计。

### ProtocolAdapter
把受控 skill 转成游戏服可理解的协议或内部请求。适配器可以参考当前客户端协议语义，但不能依赖 Unity 客户端运行时。

### ProtocolContractLayer
定义独立项目可消费的协议契约产物、版本、兼容性测试和升级流程。它是协议世界和中转层之间唯一允许的复用边界。

### AgentScheduler
控制 AI 观察、决策、执行节奏。`v1.0` 要支持多个账号的公平调度、限流和异常隔离。

### AuditLogger
记录 observation、decision、skill call、validation result、protocol request、server response、final state。

### CircuitBreaker
当出现连续失败、重复动作、非法状态、协议拒绝、会话异常时，暂停对应账号托管。

## 初始 Skill
### bootstrap_session
网关内部能力，不暴露给大模型。

职责：
- 建立托管会话。
- 完成登录。
- 进入业务方指定场景。
- 初始化 StateObserver。

说明：
- `enter_scene` 属于 bootstrap 流程或网关内部 capability，不属于 `v0.1` 默认模型可调用 skill。
- 大模型看到的第一个外部 skill 应是 `observe_state`。

### observe_state
返回当前托管角色摘要。

最低字段：
- sessionId
- traceId
- roleId
- sceneId
- position
- movementState
- controllable
- lastAction
- availableSkills

### move_to
移动到坐标或命名地点。

最低字段：
- target
- speedMode: walk | run
- stopDistance
- reason

### stop_move
停止当前移动。

最低字段：
- reason

### jump
执行一次跳跃。

最低字段：
- reason

### play_action
执行基础动作白名单中的动作。

最低字段：
- actionName
- reason

### stop_hosting
停止当前托管会话。

最低字段：
- reason

## 实施步骤
1. 梳理协议参考清单：只读整理登录、进场景、角色命令、移动、跳跃、基础动作所需协议语义。
2. 定义协议契约层：确认 source of truth、可消费产物、版本同步方式和一致性测试。
3. 定义能力模型：形成 skill metadata、schema、权限、风险等级、前置状态、限频和审计字段。
4. 设计 `DecisionProvider` 接口：允许 LLM、规则脚本、测试回放器使用同一 skill 边界。
5. 设计托管会话模型：定义 session 生命周期、登录态、心跳、重连、场景状态和错误计数。
6. 设计状态观察模型：定义 AI 可读状态摘要，避免把原始协议对象直接暴露给大模型。
7. 设计协议适配模型：把 `observe_state / move_to / stop_move / jump / play_action / stop_hosting` 映射到现有协议语义。
8. 设计 v0.1 单账号验证流：登录、进场景、观察、移动、跑、跳、动作、停止、审计。
9. 设计 v1.0 并发治理流：会话池、调度、限频、隔离、熔断、审计回放。
10. 准备交接文档：把技术栈中立接口、数据流、验收标准交给实现同事。

## 验收标准
### 指标参数表
具体数值由业务方在执行前填写；规划默认给出字段，不强制绑定数值。

| 参数 | 适用版本 | 默认占位 | 说明 |
| --- | --- | --- | --- |
| `V01_LOOP_COUNT` | v0.1 | TBD，例如 3 次 | 单账号基础行为闭环连续成功次数 |
| `V01_MAX_LOOP_DURATION` | v0.1 | TBD | 单次登录到动作闭环最长耗时 |
| `V01_ILLEGAL_PROTOCOL_SENDS` | v0.1 | 0 | 非法 skill 或非法参数导致的真实协议发送次数 |
| `V01_AUDIT_COMPLETENESS` | v0.1 | 100% | 观察、决策、skill、校验、协议、回包、状态是否全记录 |
| `V10_CONCURRENT_ACCOUNTS` | v1.0 | TBD | 并发托管账号数 |
| `V10_RUN_DURATION` | v1.0 | TBD | 并发持续运行时长 |
| `V10_MAX_DISCONNECT_RATE` | v1.0 | TBD | 允许的最大掉线率 |
| `V10_MAX_RECONNECT_TIME` | v1.0 | TBD | 单账号重连最大恢复时间 |
| `V10_ILLEGAL_PROTOCOL_SENDS` | v1.0 | 0 | 非法状态、越权或乱发动作导致的真实协议发送次数 |
| `V10_ISOLATION_SUCCESS_RATE` | v1.0 | 100% | 单账号熔断后其他账号继续运行成功率 |

### v0.1
- 使用一个测试账号完成托管授权和登录。
- 能进入业务方指定场景。
- `observe_state` 能返回当前角色位置、场景、可控状态。
- `move_to` 能让角色位置发生符合预期的变化。
- `move_to(speedMode=run)` 能以跑步语义执行移动。
- `jump` 能完成跳跃开始和结束闭环。
- `play_action` 只能执行白名单动作。
- 非白名单 skill 不发送真实协议。
- 非法参数不发送真实协议。
- 每个 skill 调用都产生审计日志。
- `v0.1` 成功以服务器可观测状态变化为准，不要求复刻 Unity 客户端本地动画、路径插值或表现层细节。
- 指标满足 `V01_*` 参数表。

### v1.0
- 多个账号可以同时建立托管会话。
- 每个账号都有独立心跳和错误计数。
- 指定并发量和运行时长内无系统性掉线。
- 不出现未登录发动作、离场景发场景动作、不可控状态发移动、重复跳跃循环等乱发行为。
- 单账号异常只熔断该账号。
- 审计日志能按账号回放一次完整托管过程。
- `v1.0` 成功以服务器可观测状态、会话稳定性和协议治理指标为准。
- 指标满足 `V10_*` 参数表。

## 验收证据格式
每次交付至少提供：

- 每账号时序日志。
- traceId、sessionId、skillCallId 关联字段。
- skill 调用次数、成功次数、拒绝次数和拒绝原因统计。
- 协议发送、协议拒发、服务器拒绝、服务器成功响应计数。
- 会话状态迁移记录。
- 熔断触发记录和恢复记录。
- v0.1 单账号闭环摘要。
- v1.0 并发运行摘要。
- 协议契约版本和一致性测试结果。

## 风险与缓解
- **协议理解偏差**：通过只读参考客户端调用链和生成协议，建立协议适配测试。
- **协议契约分叉**：通过 Protocol Contract Layer 定义 source of truth、产物版本和一致性测试。
- **大模型乱调用**：使用 skill 白名单、schema 校验、权限校验和状态前置条件。
- **并发掉线**：`v1.0` 增加心跳、重连、会话隔离和退避重试。
- **成本失控**：AgentScheduler 限制决策频率，StateObserver 控制上下文大小。
- **后续能力膨胀**：所有新能力必须先登记风险等级和权限范围。
- **误改客户端**：规划和实现文档明确当前客户端只读，不作为代码落点。

## 后续能力扩展门禁
任何新能力进入后续版本前，必须补齐：

- 风险等级。
- 权限域。
- 用户授权要求。
- 状态前置条件。
- 限频策略。
- 审计字段。
- 是否允许 LLM 直接决策。
- 是否需要确定性规则兜底。
- 是否涉及收益、资产、交易、付费、竞技或排行榜。

## 验证步骤
- 对照 `.omx/plans/test-spec-ai-offline-hosting-gateway.md` 执行 v0.1 单账号测试。
- 对照 `.omx/plans/test-spec-ai-offline-hosting-gateway.md` 执行 v1.0 并发稳定性测试。
- 审查审计日志是否覆盖观察、决策、skill 调用、校验、协议发送、回包和最终状态。
- 审查独立项目是否未修改当前 Unity 客户端工程。

## 可用 Agent 类型清单
后续如果要继续执行规划或代码实现，可按实际运行环境选择：

- `architect`: 负责独立项目边界、协议适配和扩展性审查。
- `critic`: 负责方案一致性、风险和验收标准审查。
- `executor`: 负责独立项目实现。
- `test-engineer`: 负责 v0.1 / v1.0 测试设计和验证。
- `security-reviewer`: 负责托管授权、凭证、权限和审计风险。
- `verifier`: 负责最终证据归档和验收核对。
- `writer`: 负责交接文档和实现说明。

## 后续 Staffing 建议
### Ralph 路径
适合单人顺序推进独立项目设计或原型。

建议：
- 1 个 `architect` 先冻结接口边界。
- 1 个 `executor` 实现独立项目原型。
- 1 个 `test-engineer` 编写验收测试。
- 1 个 `verifier` 按计划验收。

### Team 路径
适合多人并行推进 v1.0。

建议：
- `architect`: 负责总体架构和协议适配边界。
- `executor`: 负责会话、skill、协议适配实现。
- `test-engineer`: 负责并发压测和乱发协议测试。
- `security-reviewer`: 负责授权、权限、审计和隔离。
- `writer`: 负责对实现同事的技术栈中立交接文档。

## Launch Hints
当前规划不自动进入实现。如果后续要执行，可在独立项目仓库中使用：

```text
$ralph .omx/plans/prd-ai-offline-hosting-gateway.md
```

或使用团队路径：

```text
$team .omx/plans/prd-ai-offline-hosting-gateway.md
```

执行前必须确认目标是独立项目，而不是当前 Unity 客户端工程。

## Team Verification Path
- Team 先证明独立项目没有修改 Unity 客户端。
- Team 再证明 v0.1 单账号闭环全部通过。
- Team 最后证明 v1.0 并发运行指标通过。
- Ralph 或 Verifier 复核审计日志、熔断证据和非目标未被实现。

## 已应用审查改进记录
- Architect 审查后补充 `ProtocolContractLayer`，明确协议 source of truth、允许产物、禁止耦合方式和版本同步要求。
- Architect 审查后补充 `DecisionProvider` 抽象，避免把 v0.1 / v1.0 底座验证绑定到真实大模型。
- Architect 审查后明确 `enter_scene` 属于网关 bootstrap，不作为 v0.1 模型可调用 skill。
- Architect 审查后补充 v0.1 / v1.0 指标参数表和验收证据格式。
- Architect 审查后明确成功标准以服务器可观测状态为准，不复刻 Unity 客户端表现层细节。
- Architect 二轮审查后补充协议 owner / 同步 owner / 验收 owner 和 `traceId / sessionId / skillCallId` 审计关联字段。
- Critic 审查后补充责任人占位表，并新增面向实现同事的技术栈中立交接说明。
