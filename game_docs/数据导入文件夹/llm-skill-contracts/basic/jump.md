---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-jump
component: robotgateway-llm-skill-contracts
status: current
summary: jump 对外 LLM skill 契约，定义角色原地或移动中跳跃行为。
tags: [airobot-gateway, llm, skill, move]
last_reviewed: 2026-06-28
---

# jump

## 技能说明

让当前角色执行一次跳跃。

## 请求示例

```json
{
  "skillName": "jump",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 接收后触发一次跳跃。
- 本 skill 不暴露跳跃力度、方向、持续时长等底层控制参数。

## 并发 / 打断规则

- `jump` 可以和 `move_to` 并行。
- `jump` 不是“结束后继续 move_to”，而是允许在移动过程中直接触发一次跳跃。
- 其它首批 skill 不因为 `jump` 而自动获得并行权限。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `jump_interrupted`
- `navigation_unavailable`

## 备注

- 如果运行态不允许跳跃，由通用 `state_not_allowed` 处理。
- 通用规则见 [../common-rules.md](../common-rules.md)。
