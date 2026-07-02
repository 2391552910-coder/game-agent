---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-coffee-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: coffee_auto_schedule 面向 LLM 的固定参数表，定义咖啡名称的外部字段语义。
tags: [airobot-gateway, llm, skill, coffee, parameters]
last_reviewed: 2026-06-28
---

# coffee_auto_schedule 参数表

## 对外参数形态

```json
{
  "skillName": "coffee_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "coffeeName": "美式咖啡"
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `coffeeName` | `string` | 是 | 咖啡名称，精确匹配，不支持别名或模糊匹配。 |

## 语义补充

- 对外只暴露 `coffeeName`。
- Gateway 会把 `coffeeName` 映射到内部购买入口和道具配置。
- 如果后续需要“先坐下再喝”，应先单独调用 `seat_sit`，再调用 `coffee_auto_schedule`。

## 不对外开放的内部字段

以下字段虽然存在内部实现，但不属于 LLM v1 外部契约：

- `buy.coffeeId`
- `drink.coffeeItemId`
- `drink.isSit`
- `drink.presentationWaitMs`
- `presentationWaitMs`
