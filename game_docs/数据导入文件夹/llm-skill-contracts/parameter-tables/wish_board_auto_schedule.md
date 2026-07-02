---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-wish-board-parameter-table
component: robotgateway-llm-skill-contracts
status: current
summary: wish_board_auto_schedule 面向 LLM 的固定参数表，定义许愿牌名称匹配和许愿文本规则。
tags: [airobot-gateway, llm, skill, wish, parameters]
last_reviewed: 2026-06-28
---

# wish_board_auto_schedule 参数表

## 对外参数形态

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

## 字段表

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `boardName` | `string` | 是 | 许愿牌名称，精确匹配，不支持别名或模糊匹配。 |
| `wish` | `string` | 是 | 许愿内容，不能为空白字符串。 |

## 文本规则

- `wish` 不能为空。
- `wish` 长度不能超过 Gateway 当前配置的最大长度限制。
- 这个长度上限由 Gateway 配置决定，LLM 不应写死具体数字。

## 语义补充

- 对外不暴露内部 `configId`。
- 购买数量固定为 `1`，不由 LLM 控制。
- `boardName` 的真实可选值以后续查询能力或配置表为准。

## 不对外开放的内部字段

以下字段属于内部编排参数，不属于 LLM v1 外部契约：

- `buy.configId`
- `buy.count`
- `makeWish.configId`
- `makeWish.syncPresentation`
