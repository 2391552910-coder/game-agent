---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-shooting-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: shooting_auto_schedule 对外 LLM skill 契约，定义射击自动编排的高层参数。
tags: [airobot-gateway, llm, skill, shooting]
last_reviewed: 2026-06-28
---

# shooting_auto_schedule

## 技能说明

执行一次完整的射击自动编排流程。Gateway 负责到场、可用性检查、必要补购、开始、提交和结束。

## 请求示例

```json
{
  "skillName": "shooting_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "distance": "10m",
    "weapon": "pistol",
    "posture": "standing",
    "score": 60
  }
}
```

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `distance` | `string` | 是 | 固定业务枚举，当前按客户端现有靶距枚举。 |
| `weapon` | `string` | 是 | 固定业务枚举，当前按客户端现有枪型枚举。 |
| `posture` | `string` | 是 | 固定业务枚举，当前按客户端现有姿势枚举。 |
| `score` | `integer` | 是 | LLM 期望命中的分数，固定为 `30..80`。 |

## 执行语义

- Gateway 负责自动到活动点、检查台位、开始射击、提交成绩。
- 子弹不足时，Gateway 先判断免费次数与库存，再决定是否补购当前枪型所需子弹。
- 补购只受 Gateway 自身消费保护控制，LLM 不传预算字段。
- MyAgent 只选择参数表列出的合法 `distance + weapon + posture` 组合。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后再打枪时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `shooting_table_num_invalid`
- `shooting_distance_invalid`
- `shooting_weapon_invalid`
- `shooting_posture_invalid`
- `shooting_game_mode_invalid`
- `shooting_score_invalid`
- `shooting_score_exceeds_limit`
- `shooting_project_invalid`

## failed reasons

- `session_not_running`
- `table_occupied`
- `resource_exhausted`
- `protocol_failed`
- `cleanup_failed`

## 备注

- 本 skill 只暴露高层业务参数，不暴露项目 id、商品 id、库存 id 等内部细节。
- `distance / weapon / posture / gameMode` 的固定取值表见 [../parameter-tables/shooting_auto_schedule.md](../parameter-tables/shooting_auto_schedule.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
