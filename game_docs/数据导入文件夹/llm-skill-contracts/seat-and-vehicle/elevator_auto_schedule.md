---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-elevator-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: elevator_auto_schedule 对外 LLM skill 契约，定义电梯自动编排高层入口。
tags: [airobot-gateway, llm, skill, elevator]
last_reviewed: 2026-06-28
---

# elevator_auto_schedule

## 技能说明

执行一次完整的电梯自动编排流程。

## 请求示例

```json
{
  "skillName": "elevator_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 负责找到可用电梯并执行完整电梯流程。
- 对外不暴露 `elevatorId`、楼层 id、按钮 id、`waitMs`、`exitAfterJoin`、协议步骤或等待细节。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后坐电梯时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `elevator_available_missing`
- `elevator_start_failed`
- `elevator_wait_end_timeout`
- `elevator_protocol_error`

## 备注

- 本 skill 是高层自动编排入口，不承诺对 LLM 暴露内部楼层控制参数。
- 通用规则见 [../common-rules.md](../common-rules.md)。
