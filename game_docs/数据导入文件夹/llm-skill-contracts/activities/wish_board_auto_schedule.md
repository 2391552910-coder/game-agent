---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-wish-board-auto-schedule
component: robotgateway-llm-skill-contracts
status: current
summary: wish_board_auto_schedule 对外 LLM skill 契约，定义许愿牌自动编排的高层参数。
tags: [airobot-gateway, llm, skill, wish]
last_reviewed: 2026-06-28
---

# wish_board_auto_schedule

## 技能说明

执行一次完整的许愿牌自动编排流程。

## 请求示例

```json
{
  "skillName": "wish_board_auto_schedule",
  "schemaVersion": "v1",
  "arguments": {
    "boardName": "心愿牌",
    "wish": "希望今天顺利"
  }
}
```

## arguments 字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `boardName` | `string` | 是 | 许愿牌名称，精确匹配。 |
| `wish` | `string` | 是 | 许愿内容。 |

## 执行语义

- Gateway 按 `boardName` 精确定位要使用的许愿牌。
- 购买数量固定为 `1`，不由 LLM 控制。
- 买牌和许愿的消费保护、免费优先和累计消费限制都由 Gateway 负责。
- 对外不暴露内部 `configId`、购买协议细节或表现同步参数。

## 并发 / 打断规则

- 不允许和 `move_to` 直接并行。
- 需要移动后许愿时，LLM 先发 `stop_move`，停下后再发本 skill。

## reject reasons

- `wish_board_board_name_invalid`
- `wish_board_wish_invalid`
- `wish_board_wish_too_long`

## failed reasons

- `session_not_running`
- `wish_board_go_to_activity_failed`
- `wish_board_buy_card_blocked`
- `wish_board_buy_card_failed`
- `wish_board_make_wish_failed`
- `wish_board_protocol_error`

## 备注

- `boardName` 只允许精确匹配，不支持模糊查找、别名或近似词。
- `wish` 的长度上限由 Gateway 配置控制，不由 LLM 写死。
- 参数取值说明见 [../parameter-tables/wish_board_auto_schedule.md](../parameter-tables/wish_board_auto_schedule.md)。
- 通用规则见 [../common-rules.md](../common-rules.md)。
