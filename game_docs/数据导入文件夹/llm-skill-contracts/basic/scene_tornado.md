---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-scene-tornado
component: robotgateway-llm-skill-contracts
status: current
summary: scene_tornado 对外 LLM skill 契约，v1 为固定龙卷风场景动作。
tags: [airobot-gateway, llm, skill, scene]
last_reviewed: 2026-06-28
---

# scene_tornado

## 技能说明

触发当前场景固定龙卷风表现。

## 请求示例

```json
{
  "skillName": "scene_tornado",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 接收后执行固定龙卷风能力。
- 对外不暴露 `triggerId`、`triggerConfigId`、阶段、起点、落点、朝向、动画或其它内部控制项。
- Gateway 等待龙卷风表现和落地段结束后，同步最终落点。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要在移动后释放时，LLM 先发 `stop_move`，停下后再发 `scene_tornado`。
- `scene_tornado` 执行期间会接管角色表现，不允许其它 skill 打断。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `trigger_missing`
- `action_missing`
- `landing_unavailable`
- `protocol_failed`

## 备注

- 本 skill 首版是空参数固定技能，后续若真要开放控制项，应通过新 schemaVersion 处理。
- 通用规则见 [../common-rules.md](../common-rules.md)。
