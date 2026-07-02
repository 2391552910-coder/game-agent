---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-move-to-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: move_to 面向 LLM 的固定参数表，定义坐标移动请求、速度枚举和停止距离规则。
tags: [airobot-gateway, llm, skill, move, parameters]
last_reviewed: 2026-06-28
---

# move_to 参数表

## 对外参数形态

```json
{
  "skillName": "move_to",
  "schemaVersion": "v1",
  "arguments": {
    "target": {
      "x": 120.5,
      "y": 8.0,
      "z": -35.2
    },
    "speedMode": "walk",
    "stopDistance": 0.5
  }
}
```

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `target.x` | `number` | 是 | 目标点 X 坐标。 |
| `target.y` | `number` | 是 | 目标点 Y 坐标。 |
| `target.z` | `number` | 是 | 目标点 Z 坐标。 |
| `speedMode` | `string` | 否 | 固定枚举，见下表。默认 `walk`。 |
| `stopDistance` | `number` | 否 | 大于等于 `0`。允许传 `0`。默认 `0.5`。 |

## speedMode 枚举

| 值 | 含义 |
|---|---|
| `walk` | 走路 |
| `run` | 跑步 |

## 语义补充

- `target` 使用世界坐标。
- `stopDistance` 表示距离目标点多近时算到达，不是额外偏移量。
- `stopDistance = 0` 合法，但不保证必须与目标点完全零误差重合。

## 不对外开放的内部字段

以下字段虽然存在内部 DTO，但不属于 LLM v1 外部契约：

- `maxSnapDistance`

如果外部请求传这些字段，应按未知字段拒绝。
