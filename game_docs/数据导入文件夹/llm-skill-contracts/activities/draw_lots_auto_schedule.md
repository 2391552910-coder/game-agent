---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-draw-lots-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: draw_lots_auto_schedule 对外 LLM skill 契约，定义抽签活动自动编排入口。
tags: [airobot-gateway, llm, skill, activity]
last_reviewed: 2026-06-28
---

# draw_lots_auto_schedule

## 技能说明

执行一次完整的抽签自动编排流程。

## 请求示例

```json
{
  "skillName": "draw_lots_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 负责自动前往活动点、检查是否需要购买、发起抽签并完成流程。
- 消费判断、免费优先和购买保护由 Gateway 自己负责。
- 对外不暴露活动点、购买数量、等待时间或底层开始/结束步骤。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后抽签时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `draw_lots_go_to_activity_failed`
- `draw_lots_buy_blocked`
- `draw_lots_buy_failed`
- `draw_lots_start_failed`
- `draw_lots_protocol_error`

## 备注

- 首批只开放高层自动编排入口，不开放底层活动参数。
- 通用规则见 [../common-rules.md](../common-rules.md)。
