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
    "score": 60
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `distance` | `string` | 是 | 固定枚举，见下表。 |
| `weapon` | `string` | 是 | 固定枚举，见下表。 |
| `posture` | `string` | 是 | 固定枚举，见下表。 |
| `score` | `integer` | 是 | 固定为 `30..80`。 |

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

## 合法项目组合

`distance + weapon + posture` 不是任意组合，只允许以下 6 种：

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
- `score` 小于 `30` 或大于 `80`

这些组合应直接按 `shooting_project_invalid` 拒绝。

## 语义补充

- `score` 是 LLM 想提交的目标分数，不是命中次数。
- 子弹不足、免费次数和消费保护由 Gateway 内部处理。

## 与内部实现的关系

- Gateway 内部会把 `distance` 映射成数值项目参数。
- Gateway 内部会把 `weapon` 和 `posture` 映射成底层枪型与姿势参数。
- 这些内部映射规则不作为 LLM 侧契约字段暴露。
