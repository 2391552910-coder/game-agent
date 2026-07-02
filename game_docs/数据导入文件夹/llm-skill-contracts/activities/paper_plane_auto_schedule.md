---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-paper-plane-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: paper_plane_auto_schedule 对外 LLM skill 契约，定义纸飞机自动编排的高层参数。
tags: [airobot-gateway, llm, skill, paper-plane]
last_reviewed: 2026-06-28
---

# paper_plane_auto_schedule

## 技能说明

执行一次完整的纸飞机自动编排流程。

## 请求示例

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

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `planeName` | `string` | 是 | 纸飞机名称，精确匹配。 |
| `useTimeMs` | `integer` | 是 | 使用时长，必须落在 Gateway 允许范围内。超范围直接 reject，不做 clamp。 |
| `isComplete` | `boolean` | 是 | 是否按完整流程执行。 |

## 执行语义

- Gateway 按 `planeName` 精确匹配目标纸飞机。
- Gateway 负责自动前往活动点、必要购买、开始使用、提交结果。
- `useTimeMs` 是对外唯一开放的时间控制字段。
- `isComplete` 决定本次按完成还是未完成结果提交。
- 免费次数优先、缺资源时是否补购、补购后的消费限制都由 Gateway 负责。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后放纸飞机时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `paper_plane_plane_name_invalid`
- `paper_plane_use_time_ms_invalid`

## failed reasons

- `session_not_running`
- `paper_plane_go_to_activity_failed`
- `paper_plane_buy_item_blocked`
- `paper_plane_buy_item_failed`
- `paper_plane_start_failed`
- `paper_plane_submit_failed`
- `paper_plane_protocol_error`

## 备注

- 首批不开放 `paper_plane_synthesis`。
- `planeName` 采用精确匹配，不支持别名。
- 对外不暴露 `configId`、`itemId`、`shopId`、购买数量或等待表现细节。
- 参数取值说明见 [../parameter-tables/paper_plane_auto_schedule.md](../parameter-tables/paper_plane_auto_schedule.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
