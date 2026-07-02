---
doc_type: brainstorm-checkpoint
status: scratch
summary: LLM 可调用技能参数表与技能运行规则的后续讨论草案。
---

# LLM Gateway Skill Contract Draft

这份文件只记录后续要单独讨论的技能契约问题，不作为当前 Gateway <-> LLM HTTP 接口 v1 的运行时字段。

## 已确认边界

- 可用 skill 不通过 Gateway -> LLM runtime 事件下发，双方通过配置或提前约定维护。
- `/decision.arguments` 必须按 `skillName + schemaVersion` 对应的技能参数契约填写。
- LLM 不传通用 skill 执行 TTL；每个 skill 的执行 TTL 由 Gateway 内部配置决定。
- Gateway 对未知 skill 参数字段严格拒绝，不静默忽略。

## 后续需要逐项讨论

### 技能基础信息

- `skillName`：给 LLM 使用的稳定技能名。
- `skillName` 必须是 canonical skill name，不维护别名或同义词。
- `schemaVersion`：参数契约版本，例如 `v1`。
- 技能说明：这个 skill 做什么，不做什么。
- 适用场景：哪些 scene / account / role 状态下允许调用。

### 参数契约

- `arguments` 字段清单。
- 每个字段的类型、是否必填、默认值、取值范围。
- 坐标、角色、目标、道具等业务对象的引用方式。
- 未知字段、缺字段、类型错误时的拒绝规则。

### 执行结果契约

- `skill_finished.payload.skill.reason` 的技能专属枚举。
- 哪些 reason 表示可重试，哪些表示应该换策略。
- 需要 LLM 继续决策时，Gateway 如何给下一次 `decisionLeaseId`。
- 移动类 skill 是否登记 `target_unreachable` 这类专属失败 reason。

### 运行时规则

- 每个 skill 的 Gateway 内部执行 TTL。
- 每个 skill 是否可打断。
- 每个 skill 可被哪些 action 或 skill 抢占。
- 抢占发生时，旧 skill 如何收口，例如返回 `cancelled`。
- 当前 skill 不允许被本次新动作打断时，`/decision` 返回 `skill_in_progress`。
- 首版 `/decision` 不提供 `cancel_skill`；后续观察是否确实需要“只取消当前 skill 但不停止托管”的独立动作。

### 典型待讨论例子

- 移动中出现射击机会：Gateway 可发 `observation_updated(reason=state_changed)` 和新 `decisionLeaseId`；LLM 决策射击；Gateway 判断移动 skill 是否可被射击 skill 抢占。
- 飞镖、射击、跳舞等长流程 skill：应配置更长 Gateway 内部 TTL，不由 LLM 在 `/decision` 请求里传超时。
