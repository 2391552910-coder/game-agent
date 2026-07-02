---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-paper-plane-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: paper_plane_auto_schedule 面向 LLM 的固定参数表，定义纸飞机名称、使用时长和完成标记。
tags: [airobot-gateway, llm, skill, paper-plane, parameters]
last_reviewed: 2026-06-28
---

# paper_plane_auto_schedule 参数表

## 对外参数形态

```json
{
  "skillName": "paper_plane_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "planeName": "纸飞机A",
    "useTimeMs": 12000,
    "isComplete": true
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `planeName` | `string` | 是 | 纸飞机名称，精确匹配。 |
| `useTimeMs` | `integer` | 是 | 必须大于 `0`，且不能超过 Gateway 当前允许的最大纸飞机时长。超范围直接 reject，不做 clamp。 |
| `isComplete` | `boolean` | 是 | 是否按完成关卡结果提交。 |

## useTimeMs 规则

- `useTimeMs <= 0`：直接 reject
- `useTimeMs > Gateway 当前最大时长`：直接 reject
- 不做自动截断或 clamp

## 语义补充

- `planeName` 对外是业务名，不暴露内部 `configId`。
- `useTimeMs` 是 LLM 唯一可控制的时间字段。
- `isComplete=true` 表示按完成结果提交；`false` 表示按未完成结果提交。
- 是否购买、购买哪个内部商品、等待多久、是否沿用免费次数，都由 Gateway 内部处理。

## 不对外开放的内部字段

以下字段虽然存在内部实现，但不属于 LLM v1 外部契约：

- `presentation.configId`
- `presentation.showEffect`
- `presentation.hideEffect`
- `submit.itemId`
- `submit.isUseFree`
- `purchaseWhenNoFree`
- `waitMs`
- `waitUseTime`
