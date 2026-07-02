---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-coffee-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: coffee_auto_schedule 对外 LLM skill 契约，定义喝咖啡自动编排的高层参数。
tags: [airobot-gateway, llm, skill, coffee]
last_reviewed: 2026-06-28
---

# coffee_auto_schedule

## 技能说明

执行一次完整的买咖啡并饮用流程。

## 请求示例

```json
{
  "skillName": "coffee_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "coffeeName": "美式咖啡"
  }
}
```

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `coffeeName` | `string` | 是 | 咖啡名称，精确匹配。 |

## 执行语义

- Gateway 负责完成购买和饮用。
- 不自动查找或占用座位。
- 如果后续需要“先坐下再喝”，应由 LLM 先调用 `seat_sit`，再调用 `coffee_auto_schedule`。
- 咖啡购买、饮用和消费保护都由 Gateway 负责；LLM 不传 `coffeeItemId`、`isSit` 或表现等待参数。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后喝咖啡时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `coffee_name_invalid`

## failed reasons

- `session_not_running`
- `coffee_buy_blocked`
- `coffee_buy_failed`
- `coffee_drink_failed`
- `coffee_protocol_error`

## 备注

- `isSit` 已从首批对外参数中移除。
- 参数取值说明见 [../parameter-tables/coffee_auto_schedule.md](../parameter-tables/coffee_auto_schedule.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
