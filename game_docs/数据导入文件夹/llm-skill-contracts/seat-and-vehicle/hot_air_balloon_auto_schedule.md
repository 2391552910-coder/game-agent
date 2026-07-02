---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-hot-air-balloon-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: hot_air_balloon_auto_schedule 对外 LLM skill 契约，定义热气球自动乘坐的高层入口。
tags: [airobot-gateway, llm, skill, vehicle]
last_reviewed: 2026-06-28
---

# hot_air_balloon_auto_schedule

## 技能说明

执行一次完整的热气球自动编排流程。

## 请求示例

```json
{
  "skillName": "hot_air_balloon_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 负责找到可用热气球、上去并等待服务器推送的结束态。
- 对外不暴露热气球 id、配置 id、候选策略、表现参数等内部字段。
- 本 skill 只覆盖热气球本身流程，不负责后续楼层移动。
- 等待开始期间如果允许取消，Gateway 通过 `observation_updated(reason=vehicle_cancel_window)` 发放窄 lease。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后坐热气球时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `no_available_hot_air_balloon`
- `go_to_failed`
- `join_failed`
- `protocol_failed`

## 备注

- 如果 LLM 在取消窗口内调用 `hot_air_balloon_exit` 并被 accepted，原 `hot_air_balloon_auto_schedule` 内部取消，不单独发送原 skill 的 `skill_finished`。
- 通用规则见 [../common-rules.md](../common-rules.md)。
