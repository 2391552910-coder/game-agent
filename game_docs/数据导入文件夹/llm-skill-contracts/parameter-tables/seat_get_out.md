---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-seat-get-out-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: seat_get_out 面向 LLM 的固定参数表，定义离座时使用的 sceneId 和 chairId。
tags: [airobot-gateway, llm, skill, seat, parameters]
last_reviewed: 2026-06-28
---

# seat_get_out 参数表

## 对外参数形态

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

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `sceneId` | `integer` | 是 | 正整数。 |
| `chairId` | `integer` | 是 | 正整数。 |

## 语义补充

- 对外仍然要求显式传 `sceneId + chairId`。
- 不依赖 Gateway 根据“当前正坐着哪个座位”替 LLM 自动补外部请求参数。
- `sceneId <= 0` 或 `chairId <= 0` 都应直接 reject。
