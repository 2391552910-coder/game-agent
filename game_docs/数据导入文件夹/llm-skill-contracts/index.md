---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-contracts
component: robotgateway-llm-skill-contracts
status: current
summary: AiRobotGateway 首批对外 LLM skill 契约入口，收录公共规则、固定 reason 枚举和每个首批 skill 的独立说明页。
tags: [airobot-gateway, llm, skill, contract]
last_reviewed: 2026-06-28
---

# AiRobotGateway 首批 LLM Skill 契约

## 概述

本文档目录收录 AiRobotGateway 面向外部 LLM / DecisionProvider 的首批高层 skill 契约。目标是把“LLM 能调什么、参数长什么样、Gateway 负责哪些内部编排”固定下来，避免 LLM 直接消费底层协议步骤、内部配置 ID 或运行时临时对象。

这里的 skill 契约只描述 LLM 可见的外部表面：

- 请求固定使用 `skillName + schemaVersion + arguments`
- `arguments` 之外的多余字段一律拒绝
- skill 参数只保留高层业务语义
- 内部购买、报名、候选资源切换、协议重试和表现同步由 Gateway 负责
- 动态细节进入 Gateway 日志，不进入对外 `reason`

## 目录

- [公共规则](common-rules.md)
- [固定 reason 枚举](reason-codes.md)
- [固定参数表](parameter-tables/index.md)
- [暂不开放技能清单](not-initial-batch.md)

### 基础控制

- [move_to](basic/move_to.md)
- [stop_move](basic/stop_move.md)
- [jump](basic/jump.md)
- [play_action](basic/play_action.md)
- [scene_tornado](basic/scene_tornado.md)

### 单人活动

- [sign_in](activities/sign_in.md)
- [shooting_auto_schedule](activities/shooting_auto_schedule.md)
- [darts_auto_schedule](activities/darts_auto_schedule.md)
- [dance_auto_schedule](activities/dance_auto_schedule.md)
- [draw_lots_auto_schedule](activities/draw_lots_auto_schedule.md)
- [wish_board_auto_schedule](activities/wish_board_auto_schedule.md)
- [paper_plane_auto_schedule](activities/paper_plane_auto_schedule.md)
- [coffee_auto_schedule](activities/coffee_auto_schedule.md)

### 座位与载具

- [seat_sit](seat-and-vehicle/seat_sit.md)
- [seat_get_out](seat-and-vehicle/seat_get_out.md)
- [hot_air_balloon_auto_schedule](seat-and-vehicle/hot_air_balloon_auto_schedule.md)
- [hot_air_balloon_exit](seat-and-vehicle/hot_air_balloon_exit.md)
- [helicopter_auto_schedule](seat-and-vehicle/helicopter_auto_schedule.md)
- [helicopter_exit](seat-and-vehicle/helicopter_exit.md)
- [elevator_auto_schedule](seat-and-vehicle/elevator_auto_schedule.md)

## 首批 skill 清单

### 基础控制

- `move_to`
- `stop_move`
- `jump`
- `play_action`
- `scene_tornado`

### 单人活动

- `sign_in`
- `shooting_auto_schedule`
- `darts_auto_schedule`
- `dance_auto_schedule`
- `draw_lots_auto_schedule`
- `wish_board_auto_schedule`
- `paper_plane_auto_schedule`
- `coffee_auto_schedule`

### 座位与载具

- `seat_sit`
- `seat_get_out`
- `hot_air_balloon_auto_schedule`
- `hot_air_balloon_exit`
- `helicopter_auto_schedule`
- `helicopter_exit`
- `elevator_auto_schedule`

## 非目标

以下内容不在本目录内展开：

- Gateway 与 LLM 的事件流、lease、stateVersion 和 HMAC 细节
- 内部子 skill、底层协议步骤、配置 ID 映射
- query 详细参数表
- 多人交互活动
- 首批之外的待验证 skill
