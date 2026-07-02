---
doc_type: dev-guide
slug: airobot-gateway-llm-skill-common-rules
component: robotgateway-llm-skill-contracts
status: current
summary: AiRobotGateway 首批 LLM skill 的公共请求格式、字段校验、并发规则、消费边界和命名约束。
tags: [airobot-gateway, llm, skill, rules]
last_reviewed: 2026-06-28
---

# 公共规则

## 顶层请求格式

所有首批 LLM skill 固定使用同一层外部包装：

```json
{
  "skillName": "move_to",
  "schemaVersion": "v1",
  "arguments": {}
}
```

字段规则：

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `skillName` | `string` | 是 | 必须是 Gateway 注册并对 LLM 开放的固定 skill 名。 |
| `schemaVersion` | `string` | 是 | 当前固定 `v1`。 |
| `arguments` | `object` | 是 | 必须是对象；只允许当前 skill 文档声明的字段。无参数 skill 也必须传 `{}`。 |

## 严格校验

- `schemaVersion` 不是 `v1`，直接拒绝。
- `arguments` 不是合法对象，直接拒绝。
- `arguments` 出现未声明字段，直接拒绝。
- 顶层出现 `skillName / schemaVersion / arguments` 之外的字段，直接拒绝。
- 名称类字段全部使用精确匹配，不做别名或模糊匹配。
- 不做 trim、大小写兼容或同义词兼容。
- 重名配置视为 Gateway 配置错误，不交给 LLM 兜底。

## 命名与枚举

- 名称类字段保持业务语义，例如 `coffeeName`、`boardName`、`planeName`
- 对外优先暴露业务枚举，不暴露内部配置 ID
- 仅当业务语义天然就是数值定位时，才直接暴露数值，例如 `tableNum`、`chairId`

## 通用并发规则

当前首批 skill 的统一并发规则：

- `move_to + jump` 允许并行
- 其它 skill 不允许直接与 `move_to` 并行
- 需要执行其它 skill 时，先调用 `stop_move`
- 等角色停下后，再发目标 skill
- `stop_move` 被 accepted 后，原 `move_to` 内部取消，不单独发送原 `move_to` 的 `skill_finished`

补充：

- `play_action` v1 只表示固定挥手动作
- `play_action` 不允许在移动中直接发起
- `stop_move` 语义要求幂等；当前未移动时也应视为可接受
- 占用型长流程 skill 不提供通用 `cancel_skill / abort_skill`

## 消费与购买

所有会产生消费的路径都遵循同一原则：

- LLM 不传预算额度
- LLM 不传是否有权限购买
- LLM 不传累计消费上限
- Gateway 自己判断是否允许购买
- Gateway 自己做免费优先、库存检查、补购和累计消费保护

当前已纳入首批的内部自动补购场景：

- `shooting_auto_schedule`：子弹不足时自动补购当前枪型所需子弹
- `darts_auto_schedule`：库存不足时只补本局缺口
- `dance_auto_schedule`：活动次数不足时自动补购次数
- `wish_board_auto_schedule`：买牌受消费保护
- `paper_plane_auto_schedule`：买道具受消费保护
- `coffee_auto_schedule`：买咖啡受消费保护

## 载具取消窗口

- `hot_air_balloon_auto_schedule` 和 `helicopter_auto_schedule` 在等待开始阶段，可由 Gateway 发送 `observation_updated(reason=vehicle_cancel_window)`。
- 这张 lease 只允许：
  - `wait`
  - 配对 exit skill
  - `stop_hosting`
- 当前首批已开放的配对 exit skill 只有：
  - `hot_air_balloon_exit`
  - `helicopter_exit`
- `wait` 表示放弃这次取消机会，继续当前载具流程；不发送单独的 `wait_completed`。
- 对应 exit skill 被 accepted 后：
  - 原 auto schedule 内部取消
  - 不再单独发送原 skill 的 `skill_finished`
- exit 成功和幂等成功统一使用 `reason=ok`。

## reason 约束

- 对外 `reason` 使用固定枚举
- 不把 `roleId`、`shopId`、协议名、错误码等动态值拼进 `reason`
- 动态细节只进 Gateway 日志和审计
- 每个 skill 页只列 skill 专属 reason；通用 reason 见 [reason-codes.md](reason-codes.md)

## 首批能力边界

首批 LLM skill 只覆盖：

- 基础移动与简单动作
- 单人独立可完成的活动
- 明确座位和基础载具/电梯

首批不覆盖：

- 多人交互活动
- 低层子 skill
- query 参数细表
- 待验证或已禁用的 hosted operation
