---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-darts-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: darts_auto_schedule 面向 LLM 的固定参数表，定义飞镖类型、数量约束和值域。
tags: [airobot-gateway, llm, skill, darts, parameters]
last_reviewed: 2026-06-28
---

# darts_auto_schedule 参数表

## 对外参数形态

```json
{
  "skillName": "darts_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
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
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `score` | `integer` | 是 | 固定为 `1..50`。 |
| `darts` | `array` | 是 | 飞镖方案数组。 |
| `darts[].dartItem` | `string` | 是 | 固定枚举，见下表。 |
| `darts[].count` | `integer` | 是 | `0..9`。 |
| `allowPurchaseWhenInsufficient` | `boolean` | 是 | 固定为 `false`，MyAgent 不触发 Gateway 补购。 |

## dartItem 枚举

| 值 | 含义 |
|---|---|
| `general` | 普通飞镖 |
| `elementary` | 初级飞镖 |
| `advanced` | 高级飞镖 |

## darts 组合规则

- `darts` 中必须且只能包含 `general / elementary / advanced` 三项。
- 每种 `dartItem` 固定出现一次。
- 所有 `count` 之和必须等于 `9`。
- 任一 `count` 允许等于 `0`。

## 合法示例

### 全部普通飞镖

```json
{
  "score": 25,
  "darts": [
    {
      "dartItem": "general",
      "count": 9
    },
    {
      "dartItem": "elementary",
      "count": 0
    },
    {
      "dartItem": "advanced",
      "count": 0
    }
  ]
}
```

### 三种飞镖混用

```json
{
  "score": 40,
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
  ]
}
```

## 不合法示例

- `darts` 总数不是 `9`
- `score` 不在 `1..50`
- `allowPurchaseWhenInsufficient` 不是 `false`
- 同一个 `dartItem` 出现两次
- 缺少某个固定 `dartItem`
- 出现未定义的 `dartItem`

这些情况应直接按 `darts_dart_plan_invalid`、`darts_dart_item_invalid` 或 `darts_dart_count_invalid` 拒绝。

## 语义补充

- MyAgent 固定关闭补购；库存不足时由 Gateway 返回业务失败，不自动消费。
- 对外不开放内部道具配置 id。
