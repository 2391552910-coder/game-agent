---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-move-to
component: robotgateway-llm-skill-contracts
status: current
summary: move_to 对外 LLM skill 契约，定义目标坐标移动请求、参数范围和停止语义。
tags: [airobot-gateway, llm, skill, move]
last_reviewed: 2026-06-28
---

# move_to

## 技能说明

让当前托管角色朝指定世界坐标移动，直到进入 `stopDistance` 范围内视为完成。

## 请求示例

```json
{
  "skillName": "move_to",
  "schemaVersion": "v1",
  "arguments": {
    "target": {
      "x": 120.5,
      "y": 8.0,
      "z": -35.2
    },
    "speedMode": "walk",
    "stopDistance": 0.5
  }
}
```

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `target` | `object` | 是 | 世界坐标目标点。 |
| `target.x` | `number` | 是 | 目标点 X 坐标。 |
| `target.y` | `number` | 是 | 目标点 Y 坐标。 |
| `target.z` | `number` | 是 | 目标点 Z 坐标。 |
| `speedMode` | `string` | 否 | `walk` 或 `run`，默认 `walk`。 |
| `stopDistance` | `number` | 否 | 大于等于 `0`，默认 `0.5`。允许传 `0`。 |

## 执行语义

- Gateway 接收后开始驱动角色向目标点移动。
- 当角色进入 `stopDistance` 范围内，技能完成。
- `stopDistance = 0` 合法，但 Gateway 仍应做内部兜底，避免因为浮点或导航误差长期无法结束。

## 并发 / 打断规则

- `move_to` 可以和 `jump` 并行。
- 其它首批 skill 不允许直接和 `move_to` 并行。
- 需要切换去执行其它 skill 时，LLM 先发 `stop_move`，等停下后再发目标 skill。
- `stop_move` 被 accepted 后，原 `move_to` 内部取消，不单独发送原 `move_to` 的 `skill_finished`。

## reject reasons

- `move_to_target_invalid`
- `move_to_speed_mode_invalid`
- `move_to_stop_distance_invalid`

## failed reasons

- `session_not_running`
- `navigation_unavailable`
- `execution_limit_exceeded`

## 备注

- 本 skill 对外只暴露高层移动意图，不暴露底层导航、寻路或摇杆控制细节。
- 参数取值说明见 [../parameter-tables/move_to.md](../parameter-tables/move_to.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
