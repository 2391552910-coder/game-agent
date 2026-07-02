---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-dance-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: dance_auto_schedule 对外 LLM skill 契约，定义跳舞活动自动编排的空参数高层入口。
tags: [airobot-gateway, llm, skill, dance]
last_reviewed: 2026-06-28
---

# dance_auto_schedule

## 技能说明

执行一次完整的跳舞自动编排流程。Gateway 负责前往活动点、报名、补次数、按节奏提交成绩并等待活动结束。

## 请求示例

```json
{
  "skillName": "dance_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 内部负责：
  - 自动前往活动点
  - 自动报名
  - 次数不足时自动补购
  - 自动执行整场流程
  - 在每次可提交窗口内按内部默认规则提交成绩
- 不对外暴露 `stepCount`、`stepIntervalMs`、`startWaitMs`、`endWaitMs`、`danceStepId`、`speedPermille` 等内部参数。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后再跳舞时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `activity_point_missing`
- `go_to_failed`
- `apply_failed`
- `start_notify_timeout`
- `end_notify_timeout`
- `protocol_failed`

## 备注

- 当前对 LLM 暴露的是“安排自动跳一场”，不是节拍控制器。
- 参数取值说明见 [../parameter-tables/dance_auto_schedule.md](../parameter-tables/dance_auto_schedule.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
