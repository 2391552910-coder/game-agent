---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-dance-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: dance_auto_schedule 面向 LLM 的参数表，要求 Gateway 明确提供 score 的当前合法范围。
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
    "score": 25
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `score` | `integer` | 是 | 必须位于 Gateway 本次 `skillArgumentHints.allowedArgs` 对 `score` 提供的 `minimum..maximum` 范围内。 |

## 语义补充

- 当前 v1 表达“按指定合法分数安排自动跳一场”。
- Gateway 必须同时提供 `score.minimum` 和 `score.maximum`；缺少、类型错误或上下界颠倒时，MyAgent 不发送本 skill。
- MyAgent 不从 RAG 文档、本机配置或历史结果猜测分数范围。
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
