# 本项目与 Gateway 基础数据

## 1. Gateway 发给本项目的数据

### 1.1 基础事件数据

- `traceId`：链路追踪 ID。
- `eventId`：事件唯一 ID。
- `eventType`：事件类型，如 `session_started`、`observation_updated`、`skill_finished`。
- `sessionId`：Gateway 托管 session ID。
- `occurredAtMs`：事件发生时间。
- `stateVersion`：Gateway 当前状态版本。
- `decisionLeaseId`：本次可用于提交决策的租约 ID。

### 1.2 玩家与角色数据

- `user_id`：本项目侧玩家唯一标识。
- `accountId`：Gateway 账号 ID。
- `roleId`：Gateway 角色 ID。
- `player_name`：玩家显示名称。
- `sceneId`：当前场景 ID。
- `position`：当前位置，包含 `x / y / z`。

### 1.3 玩家快照数据

- `level`：玩家等级。
- `current_area`：当前区域或场景。
- `current_quest`：当前任务。
- `available_quests`：可执行任务列表。
- `inventory`：背包数据。
- `equipment`：装备数据。
- `stats`：玩家数值。
- `bottlenecks`：当前卡点。
- `game_specific`：项目自定义数据。

### 1.4 行为事件数据

- `behavior_event.type`：行为类型。
- `behavior_event.data.action`：玩家动作。
- `behavior_event.data.area`：发生区域。
- `behavior_event.data.position`：发生位置。
- `behavior_event.data.result`：行为结果。
- `behavior_event.data.failure_reason`：失败原因。

### 1.5 Gateway 可执行能力数据

- `availableSkills`：当前允许本项目输出的 skill 列表。
- `skillArgumentHints`：Gateway 给出的 skill 参数提示。
- `unlocked_actions`：当前允许 `play_action` 使用的动作列表。
- `lastSkillResult`：上一轮 skill 执行结果。

## 2. 本项目输出给 Gateway 的数据

### 2.1 事件接收确认

- `accepted`：是否接收成功。
- `traceId`：对应 Gateway 的追踪 ID。
- `receivedEventIds`：本次新接收的事件 ID。
- `duplicateEventIds`：重复事件 ID。

### 2.2 决策结果

- `traceId`：链路追踪 ID。
- `sessionId`：目标 Gateway session ID。
- `decisionId`：本项目生成的决策 ID。
- `decisionLeaseId`：Gateway 下发的租约 ID。
- `stateVersion`：本次决策基于的状态版本。
- `action`：决策动作，如 `call_skill`、`wait`、`no_op`、`stop_hosting`。
- `skillName`：要调用的 Gateway skill。
- `schemaVersion`：skill 参数版本，当前通常为 `v1`。
- `arguments`：skill 参数。
- `reason`：推荐原因。
- `confidence`：置信度。
- `ttlMs`：有效期。

### 2.3 当前最小 skill 输出

- `observe_state`：`{}`
- `move_to`：`{"target":{"x":number,"y":number,"z":number},"speedMode":string,"stopDistance":number}`
- `stop_move`：`{}`
- `jump`：`{}`
- `play_action`：`{"action":string}`

### 2.4 分析完成回调

- `event_type`：建议为 `analysis.completed`。
- `sessionId`：Gateway session ID。
- `user_id`：玩家唯一标识。
- `status`：分析状态。
- `reason`：分析结果原因。
- `recommended_actions`：推荐动作列表，每项包含 `skillName / schemaVersion / arguments / reason / priority / ttlMs`。
