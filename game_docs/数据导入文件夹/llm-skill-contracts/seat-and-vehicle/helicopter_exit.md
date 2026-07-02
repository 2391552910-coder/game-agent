---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-helicopter-exit
component: robotgateway-llm-skill-contracts
status: current
summary: helicopter_exit 对外 LLM skill 契约，只允许在直升机等待开始取消窗口内退出。
tags: [airobot-gateway, llm, skill, vehicle]
last_reviewed: 2026-06-28
---

# helicopter_exit

## 技能说明

在直升机等待开始取消窗口内，取消本次直升机乘坐。

这不是通用下车按钮，只允许作为 `helicopter_auto_schedule` 的配对退出 skill 使用。

## 请求示例

```json
{
  "skillName": "helicopter_exit",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- 只允许在 `observation_updated(reason=vehicle_cancel_window)` 发出的窄 lease 下调用。
- 当前挂起中的载具流程必须是 `helicopter_auto_schedule`。
- 如果 `helicopter_exit` 被 accepted，原 `helicopter_auto_schedule` 内部取消，不单独发送原 skill 的 `skill_finished`。
- 成功和幂等成功统一返回 `reason=ok`。

## 并发 / 打断规则

- 不允许在普通 lease 下调用。
- 不允许在热气球取消窗口下调用。
- 不允许打断其它普通 skill 或其它载具流程。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `protocol_failed`

## 备注

- 如果当前已经不在直升机上，但最终状态已经符合“取消成功”的目标，对外仍返回 `ok`，细节只写 Gateway 日志。
- 通用规则见 [../common-rules.md](../common-rules.md)。
