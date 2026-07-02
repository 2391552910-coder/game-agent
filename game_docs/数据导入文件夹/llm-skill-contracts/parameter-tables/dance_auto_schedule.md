---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-dance-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: dance_auto_schedule 面向 LLM 的固定参数表，说明 v1 固定为空参数对象。
tags: [airobot-gateway, llm, skill, dance, parameters]
last_reviewed: 2026-06-28
---

# dance_auto_schedule 参数表

## 对外参数形态

```json
{
  "skillName": "dance_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## 字段表

无字段。

## 语义补充

- 当前 v1 只表达“安排自动跳一场”。
- 当前 v1 不开放 `stepCount`、`stepIntervalMs`、`startWaitMs`、`endWaitMs`、`danceStepId`、`speedPermille`。
- 当前 v1 也不开放购买参数、报名参数或舞步切换参数。

## Gateway 内部负责的内容

- 前往活动点
- 报名
- 次数不足时自动补购
- 等待开场
- 逐步提交成绩
- 等待结束

## 不允许 LLM 侧补传的字段

以下字段属于 Gateway 内部编排参数，LLM 不应补传：

- `stepCount`
- `stepIntervalMs`
- `startWaitMs`
- `endWaitMs`
- `scorePerStep`
- `presentation`
- `submit.maxAllowedScore`
- `apply`
- `waiveAfterSchedule`

如果对外请求里出现这些字段，应按未知字段拒绝。
