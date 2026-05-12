# Handoff: AI 离线托管中转层交接说明

## 这是什么
这是一份 **独立项目的技术栈中立设计思路**，用于指导后续同事实现 AI 托管中转层。

它不是当前 Unity 客户端工程的改造任务，也不是要求在 `Assets/Scripts/Codes` 下新增代码。

## 核心边界
- AI 调度、中转层、skill 执行器、会话池、并发调度必须位于独立项目。
- 当前 Unity 客户端只作为只读参考源。
- 可以参考当前客户端里的协议、接口用法和调用链。
- 不允许修改当前 Unity 客户端任何文件。
- 不要求使用 Unity、ET、C# 或当前客户端技术栈。
- Python、Go、Java、C# 服务或其他服务端技术栈都可以作为实现选择。

## 推荐阅读顺序
1. `.omx/specs/deep-interview-ai-offline-hosting.md`
2. `.omx/plans/prd-ai-offline-hosting-gateway.md`
3. `.omx/plans/test-spec-ai-offline-hosting-gateway.md`

## 最重要的设计句子
大模型只负责决策，中转层负责执行约束。

更完整地说：

```text
DecisionProvider
    -> SkillRegistry
    -> SkillExecutor
    -> HostingGateway
    -> ProtocolContractLayer
    -> ProtocolAdapter
    -> Game Server
```

`DecisionProvider` 可以是大模型，也可以是规则脚本、测试回放器或人工调试器。这样实现同事可以先用确定性脚本验证网关和协议底座，再接入真实大模型。

## v0.1 做什么
`v0.1` 只证明单账号基础闭环：
- 托管授权。
- 登录。
- 进入指定场景。
- 观察状态。
- 移动。
- 跑。
- 停止移动。
- 跳。
- 基础动作白名单。
- 审计日志。

`enter_scene` 属于网关 bootstrap 或内部 capability，不默认暴露为大模型可调用 skill。

## v1.0 做什么
`v1.0` 证明多账号稳定性：
- 多账号并发登录。
- 每账号独立心跳和重连。
- 并发基础行为循环。
- 单账号异常隔离。
- 乱发协议防护。
- 熔断和恢复。
- 按账号审计回放。

## 暂时不做什么
`v0.1` 和 `v1.0` 暂不做：
- 聊天和自由文本社交。
- 任务、活动、收益领取。
- 背包、交易、付费、抽奖。
- 战斗、竞技、排行榜。
- 大模型直接拼裸协议字段。
- 修改当前 Unity 客户端。

## 协议复用怎么理解
不要让每个实现者自己猜协议。

独立项目应消费明确版本的协议契约产物，例如：
- IDL 或 schema。
- DTO / SDK / package。
- opcode 和序列化定义包。
- 经过确认的协议映射文档。

如果协议 source of truth 暂时不明确，先补协议契约清单，再开始实现。

## 验收看什么
`v0.1` 看单账号闭环是否能按审计链路证明成功。

`v1.0` 看多个账号是否能在指定并发量和运行时长下稳定运行，且没有非法真实协议发送。

成功标准以服务器可观测状态、会话稳定性和协议治理证据为准，不要求复刻 Unity 客户端本地动画、路径插值或表现层细节。
