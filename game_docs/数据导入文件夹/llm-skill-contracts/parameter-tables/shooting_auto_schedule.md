---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-shooting-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: shooting_auto_schedule 面向 LLM 的固定参数表，定义射击项目枚举、组合规则和值域。
tags: [airobot-gateway, llm, skill, shooting, parameters]
last_reviewed: 2026-06-28
---

# shooting_auto_schedule 参数表

## 对外参数形态

```json
{
  "skillName": "shooting_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "distance": "10m",
    "weapon": "pistol",
    "posture": "standing",
    "gameMode": "practice",
    "score": 86,
    "tableNum": 3,
    "allowSwitchWhenOccupied": true
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `distance` | `string` | 是 | 固定枚举，见下表。 |
| `weapon` | `string` | 是 | 固定枚举，见下表。 |
| `posture` | `string` | 是 | 固定枚举，见下表。 |
| `gameMode` | `string` | 否 | 固定枚举，见下表；默认 `practice`。 |
| `score` | `integer` | 是 | 大于等于 `0`，且不能超过 Gateway 当前射击分数上限。 |
| `tableNum` | `integer` | 否 | 正整数。具体可用台位由当前场景决定。 |
| `allowSwitchWhenOccupied` | `boolean` | 否 | `true` 表示指定台位被占用时允许 Gateway 自动换台。 |

## distance 枚举

| 值 | 含义 |
|---|---|
| `10m` | 10 米项目 |
| `25m` | 25 米项目 |
| `50m` | 50 米项目 |

## weapon 枚举

| 值 | 含义 |
|---|---|
| `pistol` | 手枪 |
| `rifle` | 步枪 |

## posture 枚举

| 值 | 含义 |
|---|---|
| `standing` | 站姿 |
| `crouching` | 蹲姿 |
| `prone` | 卧姿 |

## gameMode 枚举

| 值 | 含义 |
|---|---|
| `practice` | 练习模式 |
| `match` | 比赛模式 |
| `points` | 积分赛模式 |

## 合法项目组合

`distance + weapon + posture` 不是任意组合，当前只允许以下 6 种：

| distance | weapon | posture |
|---|---|---|
| `10m` | `pistol` | `standing` |
| `10m` | `rifle` | `standing` |
| `25m` | `pistol` | `standing` |
| `50m` | `rifle` | `standing` |
| `50m` | `rifle` | `crouching` |
| `50m` | `rifle` | `prone` |

## 不合法组合示例

- `25m + rifle + standing`
- `10m + pistol + crouching`
- `10m + rifle + prone`
- `50m + pistol + standing`

这些组合应直接按 `shooting_project_invalid` 拒绝。

## 语义补充

- `score` 是 LLM 想提交的目标分数，不是命中次数。
- 子弹不足、免费次数、补购和消费保护都由 Gateway 内部处理。

## 与内部实现的关系

- Gateway 内部会把 `distance` 映射成数值项目参数。
- Gateway 内部会把 `weapon` 和 `posture` 映射成底层枪型与姿势参数。
- Gateway 内部会把 `gameMode` 映射成具体玩法模式和等待开赛流程。
- 这些内部映射规则不作为 LLM 侧契约字段暴露。
