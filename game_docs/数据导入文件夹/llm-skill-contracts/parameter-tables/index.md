---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-parameter-tables
component: robotgateway-llm-skill-contracts
status: current
summary: AiRobotGateway 首批 LLM skill 的固定参数表入口，收录当前已定稿的首批技能对外字段说明。
tags: [airobot-gateway, llm, skill, parameters]
last_reviewed: 2026-06-28
---

# 固定参数表

## 说明

本目录只描述面向 LLM 的对外固定参数表，不描述 Gateway 内部 DTO、协议字段或配置 id。

当前已收录以下首批技能参数表：

- [move_to 参数表](move_to.md)
- [shooting_auto_schedule 参数表](shooting_auto_schedule.md)
- [darts_auto_schedule 参数表](darts_auto_schedule.md)
- [dance_auto_schedule 参数表](dance_auto_schedule.md)
- [wish_board_auto_schedule 参数表](wish_board_auto_schedule.md)
- [paper_plane_auto_schedule 参数表](paper_plane_auto_schedule.md)
- [coffee_auto_schedule 参数表](coffee_auto_schedule.md)
- [seat_sit 参数表](seat_sit.md)
- [seat_get_out 参数表](seat_get_out.md)

说明：

- 无参数 skill 不单独建参数页时，以各自 skill 页中的请求示例和 `arguments 固定为空对象 {}` 为准。
- 后续如需给 `hot_air_balloon_exit / helicopter_exit` 这类空参数 skill 单独做审核页，再补这里的索引。

## 使用原则

- LLM 只按这里声明的字段名、枚举值和值域传参。
- Gateway 自己把这些高层参数映射到内部 `start / submit / apply / presentation` 等底层结构。
- 未在这里开放的底层字段，LLM 不应自行补传。
