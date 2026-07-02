---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-not-initial-batch
component: robotgateway-llm-skill-contracts
status: current
summary: AiRobotGateway 当前未纳入首批 LLM skill 的能力清单及原因，避免后续重复讨论或误开放。
tags: [airobot-gateway, llm, skill, backlog]
last_reviewed: 2026-06-28
---

# 暂不开放技能清单

## 原则

以下能力当前不纳入首批 LLM skill：

- 多人交互活动
- 仍是底层参数语义的 auto skill
- `PendingValidation` 或 `Disabled` 的能力
- 仍需单独补 query / 参数表 / 运行态验证的能力

## 当前不纳入首批的能力

### tarot_auto_schedule

- 原因：本轮讨论已明确首批不开放塔罗

### throw_ball_auto_schedule

- 原因：多人交互流程
- 当前状态：Gateway 已按 `skill_disabled_multiplayer_untested` 禁用

### drink_wine_auto_schedule

- 原因：
  - 当前参数仍是 `itemId / wineGlassId / waitMs` 这类底层对象语义
  - 运行态仍属 `PendingValidation`
  - 还没有收敛成面向 LLM 的高层业务意图版本

### paper_plane_synthesis

- 原因：已明确暂不开放

### 低层子 skill

包括但不限于：

- `*_go_to_activity`
- `*_observe`
- `*_start`
- `*_submit_*`
- `*_presentation`
- `*_buy_*`
- `*_join`

原因：首批对 LLM 只开放高层 skill，不开放底层编排步骤。

例外：

- `hot_air_balloon_exit`
- `helicopter_exit`

这两个载具退出 skill 已纳入首批，但只允许在 `vehicle_cancel_window` 下调用。

### 未恢复的多人动作

包括但不限于：

- 真实 `throw_ball_*` 流程动作
- 真实 `gomoku_*` 对局推进动作

原因：仍需多人交互和无头验证，不适合作为默认 LLM 能力。
