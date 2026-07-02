---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-seat-sit-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: seat_sit 面向 LLM 的固定参数表，定义入座时使用的 sceneId 和 chairId。
tags: [airobot-gateway, llm, skill, seat, parameters]
last_reviewed: 2026-06-28
---

# seat_sit 参数表

## 对外参数形态

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

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `sceneId` | `integer` | 是 | 正整数。 |
| `chairId` | `integer` | 是 | 正整数。 |

## 语义补充

- `sceneId + chairId` 共同唯一定位目标座位。
- 首批不支持“自动找最近座位”。
- `sceneId <= 0` 或 `chairId <= 0` 都应直接 reject。
