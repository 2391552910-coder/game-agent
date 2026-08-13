---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-dance-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: dance_auto_schedule 面向 LLM 的参数表，定义固定的 score 合法范围。
tags: [airobot-gateway, llm, skill, dance, parameters]
last_reviewed: 2026-06-28
---

# dance_auto_schedule 参数表

## 对外参数形态

```json
{
  "skillName": "dance_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "score": 95
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `score` | `integer` | 是 | 一局跳舞的最终总分，必须是 `70..120` 的整数，包含边界。 |

## 语义补充

- 当前 v1 表达“按指定合法分数安排自动跳一场”。
- 跳舞分数范围是固定产品规则：`70..120`，MyAgent2 不依赖 Gateway 提供运行时上下限。
- `score` 不是单个舞步的分数，而是一局跳舞的最终总分。
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

除 `score` 外，以下字段属于 Gateway 内部编排参数，LLM 不应补传：

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
