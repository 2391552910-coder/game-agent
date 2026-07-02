---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-seat-sit
component: robotgateway-llm-skill-contracts
status: current
summary: seat_sit 对外 LLM skill 契约，定义按 sceneId 和 chairId 入座。
tags: [airobot-gateway, llm, skill, seat]
last_reviewed: 2026-06-28
---

# seat_sit

## 技能说明

让当前角色坐到指定座位。

## 请求示例

```json
{
  "skillName": "seat_sit",
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

- Gateway 按 `sceneId + chairId` 精确定位座位。
- 首批不支持“自动找最近座位”。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后坐下时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `seat_scene_id_invalid`
- `seat_chair_id_invalid`

## failed reasons

- `session_not_running`
- `seat_not_available`
- `seat_sit_failed`
- `seat_protocol_error`

## 备注

- 如果后续需要查询附近座位，应通过 query 能力单独扩展，不在本 skill 内隐式查找。
- 参数取值说明见 [../parameter-tables/seat_sit.md](../parameter-tables/seat_sit.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
