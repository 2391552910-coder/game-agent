---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-seat-get-out
component: robotgateway-llm-skill-contracts
status: current
summary: seat_get_out 对外 LLM skill 契约，定义按 sceneId 和 chairId 离座。
tags: [airobot-gateway, llm, skill, seat]
last_reviewed: 2026-06-28
---

# seat_get_out

## 技能说明

让当前角色从指定座位起身。

## 请求示例

```json
{
  "skillName": "seat_get_out",
  "schemaVersion": "v1",
  "arguments": {
    "sceneId": 1001,
    "chairId": 23
  }
}
```

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `sceneId` | `integer` | 是 | 座位所属场景 id。 |
| `chairId` | `integer` | 是 | 座位 id。 |

## 执行语义

- Gateway 按 `sceneId + chairId` 定位要离开的座位。
- 对外仍然保留显式座位参数，避免 LLM 误以为该 skill 会自动推断其它上下文。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后离座时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `seat_scene_id_invalid`
- `seat_chair_id_invalid`

## failed reasons

- `session_not_running`
- `seat_get_out_failed`
- `seat_protocol_error`

## 备注

- 如果当前不在该座位上，最终对外失败 reason 由 Gateway 归并到固定枚举，不回传内部细节。
- 参数取值说明见 [../parameter-tables/seat_get_out.md](../parameter-tables/seat_get_out.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
