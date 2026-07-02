---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-helicopter-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: helicopter_auto_schedule 对外 LLM skill 契约，定义直升机自动乘坐的高层入口。
tags: [airobot-gateway, llm, skill, vehicle]
last_reviewed: 2026-06-28
---

# helicopter_auto_schedule

## 技能说明

执行一次完整的直升机自动编排流程。

## 请求示例

```json
{
  "skillName": "helicopter_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 负责找到可用直升机、上去并等待服务器推送的结束态。
- 对外不暴露直升机 id、配置 id、候选策略、底层协议参数等内部字段。
- 本 skill 不负责下楼；如果后续需要下楼，由 `elevator_auto_schedule` 单独处理。
- 等待开始期间如果允许取消，Gateway 通过 `observation_updated(reason=vehicle_cancel_window)` 发放窄 lease。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后坐直升机时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `no_available_helicopter`
- `go_to_failed`
- `join_failed`
- `protocol_failed`

## 备注

- 对外是高层“坐一次直升机”，不是“控制直升机内部阶段”。
- 如果 LLM 在取消窗口内调用 `helicopter_exit` 并被 accepted，原 `helicopter_auto_schedule` 内部取消，不单独发送原 skill 的 `skill_finished`。
- 通用规则见 [../common-rules.md](../common-rules.md)。
