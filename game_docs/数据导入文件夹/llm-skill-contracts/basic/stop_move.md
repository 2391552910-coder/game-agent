---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-stop-move
component: robotgateway-llm-skill-contracts
status: current
summary: stop_move 对外 LLM skill 契约，定义停止当前移动的幂等语义。
tags: [airobot-gateway, llm, skill, move]
last_reviewed: 2026-06-28
---

# stop_move

## 技能说明

停止当前角色正在进行的移动。

## 请求示例

```json
{
  "skillName": "stop_move",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 接收后尝试停止当前移动。
- 如果当前正在执行 `move_to`，应打断该移动。
- 如果当前没有在移动，也应按幂等成功处理，不因为“本来就没动”而报错。
- `stop_move` 被 accepted 后，原 `move_to` 内部取消，不单独发送原 `move_to` 的 `skill_finished`。

## 并发 / 打断规则

- `stop_move` 的主要用途是把角色从移动态切回可执行其它 skill 的状态。
- `stop_move` 成功后，LLM 再发送下一个目标 skill。
- 当前正在执行射击、飞镖、跳舞、载具等占用型长流程 skill 时，`stop_move` 不负责打断，Gateway 返回 `skill_in_progress`。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`

## 备注

- `stop_move` 只处理移动停止语义，不承担其它活动退出、取消报名或中止载具流程的职责。
- v1 不提供通用 `cancel_skill / abort_skill`；取消长期玩法、退出活动或停止托管不复用 `stop_move`。
- 通用规则见 [../common-rules.md](../common-rules.md)。
