---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-sign-in
component: robotgateway-llm-skill-contracts
status: current
summary: sign_in 对外 LLM skill 契约，定义固定签到动作的高层入口。
tags: [airobot-gateway, llm, skill, activity]
last_reviewed: 2026-06-28
---

# sign_in

## 技能说明

执行当前场景内的签到动作。

## 请求示例

```json
{
  "skillName": "sign_in",
  "schemaVersion": "v1",
  "arguments": {}
}
```

## arguments 字段

无字段。

## 执行语义

- Gateway 接收后执行签到。
- 对外不暴露底层协议字段或内部签到步骤。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 如需在移动后签到，LLM 先发 `stop_move`，停下后再发 `sign_in`。

## reject reasons

无专属 reject。

## failed reasons

- `session_not_running`
- `sign_in_failed`
- `sign_in_protocol_error`

## 备注

- 如果失败原因来自下层协议，对外统一归并到固定枚举，不回传动态错误文本。
- 通用规则见 [../common-rules.md](../common-rules.md)。
