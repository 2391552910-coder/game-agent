---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-play-action
component: robotgateway-llm-skill-contracts
status: current
summary: play_action 对外 LLM skill 契约，v1 只表示固定挥手动作。
tags: [airobot-gateway, llm, skill, action]
last_reviewed: 2026-06-28
---

# play_action

## 技能说明

让当前角色播放一个固定动作。

v1 只支持固定挥手动作。

## 请求示例

```json
{
  "skillName": "play_action",
  "schemaVersion": "v1",
  "arguments": {
    "actionId": "wave"
  }
}
```

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `actionId` | `string` | 是 | 当前固定为 `wave`。不支持别名、大小写兼容、数字动作 ID 或底层协议 `cmdId`。 |

## 执行语义

- Gateway 接收后播放固定挥手动作。
- 对外不暴露内部动作 ID。
- 完成语义是动作指令发送成功，不等待客户端完整动画播放结束。

## 并发 / 打断规则

- `play_action` 不允许和 `move_to` 直接并行。
- 需要在移动后执行时，LLM 先发 `stop_move`，停下后再发 `play_action`。
- `play_action` v1 不与 `jump / auto_schedule` 并行，也不打断这些 skill。

## reject reasons

- `play_action_action_id_invalid`

## failed reasons

- `session_not_running`

## 备注

- 后续如果开放更多动作，优先扩展 `actionId` 枚举；是否升版本再单独判断。
- 通用规则见 [../common-rules.md](../common-rules.md)。
