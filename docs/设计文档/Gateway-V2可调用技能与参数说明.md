# Gateway V2 可调用技能与参数说明

## 1. 文档范围

本文档说明 MyAgent2 当前通过 LLM Gateway HTTP V2 向 Gateway 发送的决策动作、可调用技能及其参数要求。

当前公共合同版本为：

```text
llm-gateway-http-v2
```

技能调用使用 Gateway 在当前事件中发布的准确 `skillName` 和 `schemaVersion`。当前 MyAgent2 活动计划中的 21 个非聊天技能均按 `v1` 技能结构处理。

## 2. 决策动作

MyAgent2 可以向 Gateway 发送以下 4 种协议级决策动作：

| `action` | 中文含义 | 动作专属字段 |
|---|---|---|
| `call_skill` | 调用一个 Gateway 技能 | `skillName`、`schemaVersion`、`arguments` |
| `wait` | 等待指定时间 | `waitMs`，单位为毫秒 |
| `no_op` | 本轮不执行操作 | 无 |
| `stop_hosting` | 停止当前托管会话 | 无 |

每条决策都包含以下公共字段：

```text
traceId
contractVersion
sessionId
decisionId
decisionLeaseId
stateVersion
controlGeneration
ttlMs
action
```

`wait` 的示例：

```json
{
  "action": "wait",
  "waitMs": 1000
}
```

MyAgent2 当前安全回退生成的 `waitMs` 为 `1000` 毫秒。Gateway 是否采用其他默认等待时间，不影响 MyAgent2 显式发送的该值。

## 3. 可调用技能总表

下表中的“需要额外参数”是指 `call_skill.arguments` 是否需要包含字段。

| 序号 | `skillName` | 中文含义 | 需要额外参数 | 参数字段或来源 |
|---:|---|---|---|---|
| 1 | `observe_state` | 观察角色和场景状态 | 通常不需要 | 通常为 `{}`；仍服从 Gateway 参数提示 |
| 2 | `move_to` | 移动到指定场景坐标 | 需要 | `target.x`、`target.y`、`target.z` |
| 3 | `stop_move` | 停止当前移动 | 通常不需要 | 通常为 `{}` |
| 4 | `jump` | 跳跃 | 通常不需要 | 通常为 `{}` |
| 5 | `play_action` | 播放角色动作 | 需要 | `actionId` |
| 6 | `scene_tornado` | 通过龙卷风从初始房间进入广场 | 通常不需要 | 通常为 `{}` |
| 7 | `sign_in` | 签到 | 动态确定 | 使用 Gateway 的 `suggestedArgs`；无参数要求时为 `{}` |
| 8 | `shooting_auto_schedule` | 自动参加射击活动 | 需要 | `distance`、`weapon`、`posture`、`score` |
| 9 | `darts_auto_schedule` | 自动参加飞镖活动 | 需要 | `score`、`darts`、`allowPurchaseWhenInsufficient` |
| 10 | `dance_auto_schedule` | 自动参加跳舞活动 | 需要 | `score` |
| 11 | `draw_lots_auto_schedule` | 自动参加抽签活动 | 动态确定 | 使用 Gateway 的 `suggestedArgs`；无参数要求时为 `{}` |
| 12 | `wish_board_auto_schedule` | 自动进行许愿板活动 | 需要 | `boardName`、`wish` |
| 13 | `paper_plane_auto_schedule` | 自动参加纸飞机活动 | 需要 | `planeName`、`useTimeMs`、`isComplete` |
| 14 | `coffee_auto_schedule` | 自动进行咖啡活动 | 需要 | `coffeeName` |
| 15 | `seat_sit` | 坐到指定座位 | 需要 | `sceneId`、`chairId` |
| 16 | `seat_get_out` | 从指定座位起身 | 需要 | `sceneId`、`chairId` |
| 17 | `hot_air_balloon_auto_schedule` | 自动乘坐热气球 | 动态确定 | 使用 Gateway 的 `suggestedArgs`；无参数要求时为 `{}` |
| 18 | `hot_air_balloon_exit` | 退出热气球流程 | 通常不需要 | 通常为 `{}`；需要对应载具 lease |
| 19 | `helicopter_auto_schedule` | 自动乘坐直升机 | 动态确定 | 使用 Gateway 的 `suggestedArgs`；无参数要求时为 `{}` |
| 20 | `helicopter_exit` | 退出直升机流程 | 通常不需要 | 通常为 `{}`；需要对应载具 lease |
| 21 | `elevator_auto_schedule` | 自动乘坐电梯 | 动态确定 | 使用 Gateway 的 `suggestedArgs`；无参数要求时为 `{}` |

## 4. 固定参数技能

### 4.1 移动 `move_to`

必需参数：

```json
{
  "target": {
    "x": 108.0,
    "y": 0.0,
    "z": 125.0
  }
}
```

约束：

- `x`、`y`、`z` 必须是有效数字；
- 坐标必须来自当前场景的可信场景配置；
- 当前场景必须与目标点所属场景一致；
- 不允许由模型编造不存在的坐标；
- `stopDistance` 等附加字段只有在 Gateway 的 `allowedArgs` 明确允许时才能发送。

### 4.2 播放动作 `play_action`

必需参数：

```json
{
  "actionId": "wave"
}
```

约束：

- `actionId` 必须是非空字符串；
- V2 不接受旧字段 `action`；
- 实际动作编号或名称必须来自 Gateway 参数提示或可信角色动作数据。

### 4.3 射击 `shooting_auto_schedule`

参数示例：

```json
{
  "distance": "10m",
  "weapon": "pistol",
  "posture": "standing",
  "score": 50
}
```

`score` 由 MyAgent2 生成，范围为 `30` 至 `80` 的整数。

合法项目组合：

| `distance` | `weapon` | `posture` |
|---|---|---|
| `10m` | `pistol` | `standing` |
| `10m` | `rifle` | `standing` |
| `25m` | `pistol` | `standing` |
| `50m` | `rifle` | `standing` |
| `50m` | `rifle` | `crouching` |
| `50m` | `rifle` | `prone` |

### 4.4 飞镖 `darts_auto_schedule`

参数示例：

```json
{
  "score": 25,
  "darts": [
    {
      "dartItem": "general",
      "count": 3
    },
    {
      "dartItem": "elementary",
      "count": 3
    },
    {
      "dartItem": "advanced",
      "count": 3
    }
  ],
  "allowPurchaseWhenInsufficient": false
}
```

约束：

- `score` 是 `1` 至 `50` 的整数；
- `darts` 必须依次包含 `general`、`elementary`、`advanced`；
- 三类飞镖的 `count` 都是 `0` 至 `9` 的整数；
- 三类飞镖的 `count` 总和必须等于 `9`；
- `allowPurchaseWhenInsufficient` 固定为 `false`。

### 4.5 跳舞 `dance_auto_schedule`

参数示例：

```json
{
  "score": 95
}
```

当前代码生成的 `score` 为 `70` 至 `120` 的整数。

### 4.6 纸飞机 `paper_plane_auto_schedule`

参数示例：

```json
{
  "planeName": "初级",
  "useTimeMs": 150000,
  "isComplete": true
}
```

参数约束：

| `planeName` | `useTimeMs` 最小值 | `useTimeMs` 最大值 |
|---|---:|---:|
| `初级` | 100000 | 200000 |
| `中级` | 90000 | 180000 |
| `高级` | 70000 | 130000 |

- `planeName` 只能是 `初级`、`中级`、`高级`；
- `useTimeMs` 的单位是毫秒，不是秒；
- `isComplete` 固定为 `true`。

## 5. Gateway 提示参数技能

以下技能的实际参数主要来自当前事件中的 `skillArgumentHints.suggestedArgs`：

### 5.1 许愿板 `wish_board_auto_schedule`

```json
{
  "boardName": "wish-board-1",
  "wish": "祝今天一切顺利"
}
```

- `boardName` 和 `wish` 都必须是非空字符串；
- 具体可用许愿板名称必须由 Gateway 提供。

### 5.2 咖啡 `coffee_auto_schedule`

```json
{
  "coffeeName": "latte"
}
```

- `coffeeName` 必须是非空字符串；
- 具体可用咖啡名称必须由 Gateway 提供。

### 5.3 入座和起身

`seat_sit`：

```json
{
  "sceneId": 7,
  "chairId": 1
}
```

`seat_get_out` 使用相同字段：

```json
{
  "sceneId": 7,
  "chairId": 1
}
```

约束：

- `sceneId` 和 `chairId` 必须是大于 `0` 的整数；
- 坐下和起身必须指向同一座位；
- `seat_get_out` 只能在角色已经成功坐下后执行；
- 实际座位编号必须来自 Gateway 或可信场景状态。

### 5.4 其他动态参数技能

以下技能没有 MyAgent2 本地硬编码的固定参数结构：

```text
sign_in
draw_lots_auto_schedule
hot_air_balloon_auto_schedule
helicopter_auto_schedule
elevator_auto_schedule
```

处理规则：

1. Gateway 没有要求参数时，MyAgent2 可以发送空对象 `{}`；
2. Gateway 提供 `suggestedArgs` 时，MyAgent2 使用建议参数；
3. 参数字段必须全部出现在 `allowedArgs` 中；
4. `missingArgs` 声明的必填路径必须全部存在；
5. 参数不完整或不合法时，MyAgent2 不发送该技能，改选其他合法技能或返回 `wait`。

## 6. 无本地固定参数技能

以下技能在当前实现中通常发送空参数：

```text
observe_state
stop_move
jump
scene_tornado
hot_air_balloon_exit
helicopter_exit
```

示例：

```json
{
  "action": "call_skill",
  "skillName": "jump",
  "schemaVersion": "v1",
  "arguments": {}
}
```

“通常发送 `{}`”不表示 Gateway 永远不能为这些技能增加参数要求。当前事件中的 `skillArgumentHints` 始终是本次调用的最终参数约束。

## 7. 技能可执行条件

技能出现在 MyAgent2 的技能目录中，不代表任意时刻都可以发送。每次决策必须同时满足：

1. Gateway lease 的 `allowedActions` 包含 `call_skill`；
2. 技能出现在当前 `availableSkills` 中；
3. `skillName` 和 `schemaVersion` 与 Gateway 发布内容完全一致；
4. 技能处于当前 lease 允许的 `allowedSkillName` 或 `allowedSkillNames` 范围；
5. `arguments` 满足 `allowedArgs`、`missingArgs` 和本地合同校验；
6. `decisionLeaseId`、`stateVersion` 和 `controlGeneration` 与当前事件一致。

不同 lease 的附加限制：

| `leaseKind` | 可执行范围 |
|---|---|
| `observation` | 当前 `availableSkills` 中允许的普通活动技能，不允许直接调用载具退出技能 |
| `movement_control` | 仅允许 `jump` 或 `stop_move`；`stop_move` 的父技能必须是 `move_to` |
| `vehicle_cancel_window` | 仅允许退出当前正在执行的对应载具 |
| `vehicle_recovery` | 允许 `observe_state` 或退出当前对应载具 |
| `conversation` | 仅允许聊天相关处理，不进入普通活动计划 |

载具退出技能必须成对使用：

```text
hot_air_balloon_auto_schedule -> hot_air_balloon_exit
helicopter_auto_schedule      -> helicopter_exit
```

## 8. 完整 `call_skill` 决策示例

```json
{
  "traceId": "trace-example-1",
  "contractVersion": "llm-gateway-http-v2",
  "sessionId": "3574531302836404224",
  "decisionId": "decision-example-1",
  "decisionLeaseId": "lease-example-1",
  "stateVersion": 12,
  "controlGeneration": 822,
  "ttlMs": 30000,
  "action": "call_skill",
  "skillName": "paper_plane_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "planeName": "中级",
    "useTimeMs": 120000,
    "isComplete": true
  }
}
```

## 9. 聊天发送说明

聊天不属于上述 21 个普通活动计划技能。MyAgent2 收到 `chat_received` 或 `nearby_friend_chat_requested` 事件后，通过 Gateway 独立聊天接口发送内容：

```text
POST /api/v1/hosting/llm/chat/send
```

请求体包含：

```json
{
  "sessionId": "3574531302836404224",
  "targetAvatarId": "10001",
  "targetRoleId": "20001",
  "chatType": "nearby",
  "content": "你好，今天广场很热闹。"
}
```

聊天请求需要 `sessionId`、`targetAvatarId`、`targetRoleId`、`chatType` 和 `content`，并使用独立的聊天发送结果事件确认最终状态。

## 10. 强制技能测试配置

`.env` 中可以使用以下配置限制联调期间优先测试的技能：

```env
LLM_GATEWAY_V2_FORCE_SKILLS=paper_plane_auto_schedule,darts_auto_schedule
```

该配置只控制测试技能选择，不会绕过 Gateway lease、`availableSkills`、参数提示、状态版本或决策回调校验。

