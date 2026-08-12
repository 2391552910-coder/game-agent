---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-darts-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: darts_auto_schedule 对外 LLM skill 契约，定义飞镖自动编排的高层参数。
tags: [airobot-gateway, llm, skill, darts]
last_reviewed: 2026-06-28
---

# darts_auto_schedule

## 技能说明

执行一次完整的飞镖自动编排流程。Gateway 负责检查镖盘、补足当前局飞镖、开始投掷并提交结果。

## 请求示例

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

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `score` | `integer` | 是 | 期望总分，固定为 `1..50`。 |
| `darts` | `array` | 是 | 本局使用的飞镖方案。 |
| `darts[].dartItem` | `string` | 是 | `general`、`elementary`、`advanced`。 |
| `darts[].count` | `integer` | 是 | 当前类型飞镖数量，允许 `0`。 |
| `allowPurchaseWhenInsufficient` | `boolean` | 是 | 固定为 `false`，MyAgent 不触发自动补购。 |

## 执行语义

- `darts` 中每种 `dartItem` 固定出现一次。
- `darts` 的总数量必须等于 `9`。
- 三种 `count` 允许为 `0`。
- 飞镖不足时由 Gateway 返回业务失败，MyAgent 不自动购买。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后再打飞镖时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `darts_dart_pos_invalid`
- `darts_score_invalid`
- `darts_score_exceeds_limit`
- `darts_dart_item_invalid`
- `darts_dart_count_invalid`
- `darts_dart_plan_invalid`

## failed reasons

- `session_not_running`
- `dart_pos_occupied`
- `no_available_dart_pos`
- `insufficient_darts`
- `insufficient_entry_cost`
- `spending_limit_exceeded`
- `purchase_failed`
- `protocol_failed`
- `cleanup_failed`

## 备注

- 本 skill 只暴露业务语义，不暴露内部道具 id、购买协议字段或底层投掷步骤。
- `dartItem` 的固定取值和组合规则见 [../parameter-tables/darts_auto_schedule.md](../parameter-tables/darts_auto_schedule.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
