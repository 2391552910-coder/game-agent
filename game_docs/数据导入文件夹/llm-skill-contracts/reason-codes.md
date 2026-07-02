---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-reason-codes
component: robotgateway-llm-skill-contracts
status: current
summary: AiRobotGateway 首批 LLM skill 的固定 reason 枚举表，包含通用 reason 以及各 skill 的 reject / failed reason。
tags: [airobot-gateway, llm, skill, reason]
last_reviewed: 2026-06-28
---

# 固定 reason 枚举

## 说明

- 本文档只列对外固定枚举。
- 动态细节不进入对外 `reason`。
- 成功统一使用 `ok`。
- 这里只收录首批 skill 已确认的专属 reason；更高层通用拒绝原因仍以 Gateway 外部决策契约为准。

## 通用 reason

### 通用接受 / 终态

- `ok`

### 通用拒绝 / 失败

- `lease_expired`
- `lease_not_found`
- `lease_session_mismatch`
- `stale_state`
- `skill_in_progress`
- `skill_not_allowed`
- `skill_not_found`
- `schema_invalid`
- `state_not_allowed`
- `session_not_running`
- `circuit_breaker_open`
- `ttl_expired`
- `rate_limited`
- `idempotency_key_conflict`

## 基础控制

### move_to

- reject
  - `move_to_target_invalid`
  - `move_to_speed_mode_invalid`
  - `move_to_stop_distance_invalid`
- failed
  - `navigation_unavailable`
  - `execution_limit_exceeded`

### stop_move

- reject
  - 无专属 reject
- failed
  - 无专属 failed

### jump

- reject
  - 无专属 reject
- failed
  - `jump_interrupted`
  - `navigation_unavailable`

### play_action

- reject
  - `play_action_action_id_invalid`
- failed
  - 无专属 failed

### scene_tornado

- reject
  - 无专属 reject
- failed
  - `trigger_missing`
  - `action_missing`
  - `landing_unavailable`
  - `protocol_failed`

## 单人活动

### sign_in

- reject
  - 无专属 reject
- failed
  - `sign_in_failed`
  - `sign_in_protocol_error`

### shooting_auto_schedule

- reject
  - `shooting_table_num_invalid`
  - `shooting_distance_invalid`
  - `shooting_weapon_invalid`
  - `shooting_posture_invalid`
  - `shooting_game_mode_invalid`
  - `shooting_score_invalid`
  - `shooting_score_exceeds_limit`
  - `shooting_project_invalid`
- failed
  - `table_occupied`
  - `resource_exhausted`
  - `protocol_failed`
  - `cleanup_failed`

### darts_auto_schedule

- reject
  - `darts_dart_pos_invalid`
  - `darts_score_invalid`
  - `darts_score_exceeds_limit`
  - `darts_dart_item_invalid`
  - `darts_dart_count_invalid`
  - `darts_dart_plan_invalid`
- failed
  - `dart_pos_occupied`
  - `no_available_dart_pos`
  - `insufficient_darts`
  - `insufficient_entry_cost`
  - `spending_limit_exceeded`
  - `purchase_failed`
  - `protocol_failed`
  - `cleanup_failed`

### dance_auto_schedule

- reject
  - 无专属 reject
- failed
  - `activity_point_missing`
  - `go_to_failed`
  - `apply_failed`
  - `start_notify_timeout`
  - `end_notify_timeout`
  - `protocol_failed`

### draw_lots_auto_schedule

- reject
  - 无专属 reject
- failed
  - `draw_lots_go_to_activity_failed`
  - `draw_lots_buy_blocked`
  - `draw_lots_buy_failed`
  - `draw_lots_start_failed`
  - `draw_lots_protocol_error`

### wish_board_auto_schedule

- reject
  - `wish_board_board_name_invalid`
  - `wish_board_wish_invalid`
  - `wish_board_wish_too_long`
- failed
  - `wish_board_go_to_activity_failed`
  - `wish_board_buy_card_blocked`
  - `wish_board_buy_card_failed`
  - `wish_board_make_wish_failed`
  - `wish_board_protocol_error`

### paper_plane_auto_schedule

- reject
  - `paper_plane_plane_name_invalid`
  - `paper_plane_use_time_ms_invalid`
- failed
  - `paper_plane_go_to_activity_failed`
  - `paper_plane_buy_item_blocked`
  - `paper_plane_buy_item_failed`
  - `paper_plane_start_failed`
  - `paper_plane_submit_failed`
  - `paper_plane_protocol_error`

### coffee_auto_schedule

- reject
  - `coffee_name_invalid`
- failed
  - `coffee_buy_blocked`
  - `coffee_buy_failed`
  - `coffee_drink_failed`
  - `coffee_protocol_error`

## 座位与载具

### seat_sit

- reject
  - `seat_scene_id_invalid`
  - `seat_chair_id_invalid`
- failed
  - `seat_not_available`
  - `seat_sit_failed`
  - `seat_protocol_error`

### seat_get_out

- reject
  - `seat_scene_id_invalid`
  - `seat_chair_id_invalid`
- failed
  - `seat_get_out_failed`
  - `seat_protocol_error`

### hot_air_balloon_auto_schedule

- reject
  - 无专属 reject
- failed
  - `no_available_hot_air_balloon`
  - `go_to_failed`
  - `join_failed`
  - `protocol_failed`

### hot_air_balloon_exit

- reject
  - 无专属 reject
- failed
  - `protocol_failed`

### helicopter_auto_schedule

- reject
  - 无专属 reject
- failed
  - `no_available_helicopter`
  - `go_to_failed`
  - `join_failed`
  - `protocol_failed`

### helicopter_exit

- reject
  - 无专属 reject
- failed
  - `protocol_failed`

### elevator_auto_schedule

- reject
  - 无专属 reject
- failed
  - `elevator_available_missing`
  - `elevator_start_failed`
  - `elevator_wait_end_timeout`
  - `elevator_protocol_error`
