# RobotGateway Protobuf TCP Migration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 myAgent2 与 RobotGateway 的主通信链路从 HTTP Webhook / HTTP 拉取 / HTTP Callback 改为 AI 决策层主动发起的 protobuf + TCP 持久连接，并每 5 分钟请求游戏状态、执行 LangGraph 决策、返回一条推荐动作及原因。

**Architecture:** 新增一个常驻 TCP 决策客户端作为 RobotGateway 主链路入口。客户端启动后初始化 DB、Redis、RAG、LLM 等基础设施，主动连接 RobotGateway，完成握手、鉴权和心跳；随后按 300 秒周期发送游戏状态请求，将返回的 protobuf 状态转换为 LangGraph 输入，执行现有决策图，最后通过同一条 TCP 连接发送 `DecisionResult` 并等待 ACK。

**Tech Stack:** Python 3.11、asyncio TCP streams、protobuf、LangGraph、LangChain、LightRAG、PostgreSQL、Redis、现有 myAgent2 agent/service 代码。

---

## 1. 当前结论

当前项目与目标差距较大，不是简单替换 HTTP 客户端库，而是要把 RobotGateway 集成层从 HTTP 事件架构改成常驻 TCP 决策客户端架构。

现有 RobotGateway 相关主链路是：

```text
RobotGateway
  -> HTTP POST /webhooks/player-event
  -> FastAPI
  -> Redis 去重
  -> Prefect analysis_flow
  -> LangGraph
  -> PostgreSQL 存储结果
  -> HTTP POST RobotGateway callback
```

目标链路应调整为：

```text
AI 决策层常驻进程
  -> 主动连接 RobotGateway TCP
  -> Handshake / Auth / Heartbeat
  -> 每 300 秒发送 GameStateRequest
  <- 接收 GameStateResponse
  -> 执行 LangGraph 决策
  -> 发送 DecisionResult
  <- 接收 Ack
```

## 2. 当前差距

| 目标 | 当前实现 | 差距 |
|---|---|---|
| protobuf + TCP | `pyproject.toml` 依赖 FastAPI、Uvicorn、httpx，没有 protobuf 运行时、proto 文件、TCP 编解码层 | 需要新增 `.proto`、生成代码、TCP framing、连接管理 |
| AI 决策层主动连接 RobotGateway | `src/api/routes/webhooks.py` 中 RobotGateway 通过 HTTP Webhook 调用 myAgent2 | 连接方向完全相反 |
| 持久连接 | 当前是短连接 HTTP：快照 GET、结果 POST | 需要常驻客户端、心跳、断线重连、请求响应关联、ACK |
| 每 5 分钟主动询问 | 当前是 `offline` 事件触发 `src/core/scheduler/triggers.py::schedule_offline_analysis`，通过 Redis 去重和 Prefect 执行 | 需要轮询循环替代事件驱动调度 |
| 获取游戏状态 | `src/game_specific/connector.py::_fetch_robotgateway_snapshot` 通过 HTTP GET `/players/{user_id}/snapshot` 拉取 | 要改为 TCP protobuf `GameStateRequest` / `GameStateResponse` |
| 返回推荐动作 | `src/core/integration/robotgateway_callback.py::send_robotgateway_analysis_callback` 通过 HTTP POST 回调 | 要改为同一 TCP 连接发送 `DecisionResult` |
| 只返回一条动作和原因 | `src/core/agents/models.py::PlayerAnalysisOutput` 输出 `recommended_actions: list[...]`，prompt 也要求 `actions` 数组 | 输出模型、prompt、追踪逻辑都要改为单动作 |
| LangGraph 决策 | `src/core/agents/orchestrator.py::build_orchestrator` 已有可复用主图 | 主要改输入、输出、运行时，不需要推倒 LangGraph |
| 进程启动 | 当前通常启动 FastAPI 服务和 Prefect Worker | 需要新增 `robotgateway_tcp_client` 常驻入口；FastAPI / Prefect 不再是 RobotGateway 主链路 |

## 3. 推荐目标架构

```text
src/runtime/robotgateway_decision_client.py
  |
  |-- init_db()
  |-- init_redis()
  |-- initialize LLM provider / RAG as needed
  |
  |-- RobotGatewayTcpClient
      |
      |-- connect(host, port)
      |-- send ClientHello
      |-- receive ServerHello
      |-- start heartbeat loop
      |-- start read loop
      |-- every 300s:
          |
          |-- send GameStateRequest
          |-- receive GameStateResponse
          |-- run_decision_cycle(...)
          |-- send DecisionResult
          |-- receive Ack
```

核心原则：

- RobotGateway 通信主链路不再依赖 HTTP。
- AI 决策层是 TCP 客户端，RobotGateway 是 TCP 服务端。
- 一条持久连接承载状态请求、状态响应、决策结果、ACK、心跳和错误消息。
- LangGraph 决策能力保留，外部输入输出协议重做。
- 每个决策结果必须有 `request_id` / `decision_id`，并等待 RobotGateway ACK，避免结果丢失。

## 4. 协议设计方案

### 4.1 新增 proto 文件

建议新增：

```text
proto/robotgateway/decision.proto
```

建议定义以下消息：

- `Envelope`
- `ClientHello`
- `ServerHello`
- `Heartbeat`
- `GameStateRequest`
- `GameStateResponse`
- `DecisionResult`
- `DecisionAck`
- `ProtocolError`

### 4.2 Envelope 字段

`Envelope` 应至少包含：

- `protocol_version`
- `message_type`
- `request_id`
- `sequence`
- `timestamp_ms`
- `client_id`
- `tenant_id`
- oneof payload

原因：

- `request_id` 用于关联请求、响应和 ACK。
- `sequence` 用于排查乱序、重复发送和断线重连问题。
- `protocol_version` 用于后续协议升级。
- `tenant_id` 替代原 HTTP `X-API-Key -> request.state.tenant_id` 链路。

### 4.3 TCP framing

建议使用：

```text
4 字节 big-endian unsigned length + protobuf Envelope bytes
```

必须实现：

- 最大帧长度限制，例如 `ROBOTGATEWAY_MAX_FRAME_BYTES`。
- 半包读取。
- 多包连续读取。
- protobuf decode error 转换为协议错误。
- 读写超时。

不要直接在 TCP 流里拼接 JSON 字符串，也不要依赖换行符分隔消息。

### 4.4 GameStateResponse 内容

`GameStateResponse` 至少应能表达：

- `tenant_id`
- `user_id`
- `snapshot`
- `recent_events`
- `game_context`
- `state_version`
- `observed_at`

如果游戏状态字段还不稳定，第一阶段可以用 protobuf `Struct` 承载 `snapshot` 和 `game_specific`，但推荐将稳定字段逐步收敛为强类型 proto 字段。

### 4.5 DecisionResult 内容

`DecisionResult` 至少应包含：

- `decision_id`
- `request_id`
- `tenant_id`
- `user_id`
- `recommended_action`
- `reason`
- `confidence`
- `generated_at`
- `model_info`
- `diagnostics`

`recommended_action` 应包含：

- `action_type`
- `priority`
- `payload`
- `goal_metric`
- `goal_value`
- `expected_hours`

其中 `reason` 是 RobotGateway 明确要求返回的“选择这个动作的原因”。

## 5. 文件级修改方案

### 5.1 新增 protobuf 与生成代码

建议新增：

```text
proto/robotgateway/decision.proto
src/generated/robotgateway/decision_pb2.py
```

`decision_pb2.py` 应通过生成命令产生，不手写。

需要修改：

```text
pyproject.toml
uv.lock
```

新增依赖建议：

- `protobuf`
- 用于开发期生成代码的 `grpcio-tools`，或项目选定的等价生成工具

这里不建议引入 gRPC，因为目标是 protobuf + TCP，不是 HTTP/2 gRPC。

### 5.2 新增 TCP 集成层

建议新增目录：

```text
src/core/integration/robotgateway_tcp/
```

建议文件：

```text
src/core/integration/robotgateway_tcp/__init__.py
src/core/integration/robotgateway_tcp/codec.py
src/core/integration/robotgateway_tcp/client.py
src/core/integration/robotgateway_tcp/messages.py
src/core/integration/robotgateway_tcp/errors.py
```

职责划分：

- `codec.py`：长度帧编码、长度帧读取、protobuf 序列化和反序列化。
- `client.py`：TCP 连接、握手、心跳、读循环、写锁、pending request、重连。
- `messages.py`：项目 dict / Pydantic 模型与 protobuf 消息之间的转换。
- `errors.py`：连接错误、协议错误、超时错误、ACK 错误。

### 5.3 替换 RobotGateway HTTP 快照获取

当前待替换文件：

```text
src/game_specific/connector.py
tests/unit/test_robotgateway_http_source.py
```

当前 `_fetch_robotgateway_snapshot` 通过 HTTP 获取快照，目标方案中应废弃。

建议新增：

```text
src/core/decision/state_provider.py
```

职责：

- 调用 `RobotGatewayTcpClient.request_game_state(...)`。
- 将 `GameStateResponse` 转换为 LangGraph 所需的 `snapshot: dict`。
- 将 `recent_events` 转换为现有动态决策系统可消费的数据。

如果仍保留 `fetch_player_snapshot(user_id)` 作为内部抽象，也应该让它委托给 TCP state provider，而不是 HTTP。

### 5.4 抽出决策服务

当前 `src/core/scheduler/flows/analysis_flow.py` 同时承担 Prefect task、快照获取、LangGraph 执行、存储、callback。

建议新增：

```text
src/core/decision/service.py
```

建议函数：

```python
async def run_decision_cycle(
    *,
    tenant_id: str,
    user_id: str,
    snapshot: dict,
    recent_events: list[dict] | None = None,
) -> dict:
    ...
```

职责：

- 写入或加载 recent session events。
- 执行 LangGraph。
- 校验最终输出必须包含一条推荐动作。
- 存储分析结果。
- 返回适合 protobuf `DecisionResult` 的 dict。

Prefect Flow 可以短期保留，但它应调用 `run_decision_cycle`，不要再拥有 RobotGateway 主链路逻辑。

### 5.5 改 LangGraph 输出为单动作

当前相关文件：

```text
src/core/agents/models.py
src/core/agents/prompts.py
src/core/agents/nodes.py
tests/unit/test_models.py
tests/unit/test_nodes.py
```

需要调整：

- `ActionList` 不再作为最终行动推理输出包装。
- `PlayerAnalysisOutput.recommended_actions: list[RecommendedAction]` 改为单动作字段。
- prompt 从“JSON 顶层必须是 actions 数组”改为“JSON 顶层必须是一条 recommended_action”。
- `merge_output_node` 不再组装列表。
- `tracking_update_node` 不再遍历 action list，而是处理单个 action。

建议模型：

```python
class PlayerDecisionOutput(BaseModel):
    player_profile: BehaviorProfile
    recommended_action: RecommendedAction
    reason: str
```

如果 `RecommendedAction.reason` 已足够表达原因，也可以不重复 `reason` 字段，但为了满足 RobotGateway 协议清晰性，建议在最终输出层保留 `reason`，并要求它与动作原因一致或更完整。

### 5.6 新增常驻运行时

建议新增：

```text
src/runtime/__init__.py
src/runtime/robotgateway_decision_client.py
scripts/run_robotgateway_tcp_client.cmd
```

运行时职责：

- 初始化 DB / Redis。
- 初始化 LLM provider。
- 创建并保持 TCP 连接。
- 按 `ROBOTGATEWAY_POLL_INTERVAL_SECONDS=300` 调用决策循环。
- 控制同一连接上的 single-flight，避免上一次 LangGraph 决策尚未完成时开启下一次。
- 优雅关闭时关闭 Redis、DB 和 TCP writer。

### 5.7 修改配置

当前 RobotGateway HTTP 配置在 `src/config.py` 中：

- `robotgateway_base_url`
- `robotgateway_snapshot_api_key`
- `robotgateway_snapshot_timeout_seconds`
- `robotgateway_callback_url`
- `robotgateway_callback_timeout_seconds`
- `robotgateway_callback_api_key`

建议替换为：

- `robotgateway_tcp_host`
- `robotgateway_tcp_port`
- `robotgateway_auth_token`
- `robotgateway_client_id`
- `robotgateway_protocol_version`
- `robotgateway_poll_interval_seconds`
- `robotgateway_connect_timeout_seconds`
- `robotgateway_request_timeout_seconds`
- `robotgateway_heartbeat_interval_seconds`
- `robotgateway_reconnect_initial_delay_seconds`
- `robotgateway_reconnect_max_delay_seconds`
- `robotgateway_max_frame_bytes`

`.env.example` 也要同步替换。

### 5.8 修改部署和启动方式

当前开发启动围绕：

- `uvicorn src.api.main:app`
- `scripts/serve_analysis_flow.py`
- `scripts/run_api_robotgateway.cmd`
- `scripts/run_analysis_flow_serve.cmd`

目标主链路应新增：

```text
python -m src.runtime.robotgateway_decision_client
```

`docker-compose.dev.yml` 当前主要是基础设施服务。建议新增一个可选 app service：

```text
ai-decision-client
  command: python -m src.runtime.robotgateway_decision_client
  depends_on:
    postgres
    redis
    milvus
```

如果项目整体彻底不提供 HTTP，则 FastAPI app service 不再作为部署对象；如果只是 RobotGateway 主链路不走 HTTP，则 FastAPI 可以保留为内部管理面。

## 6. 可靠性方案

### 6.1 心跳

TCP 客户端应定期发送 `Heartbeat`。

RobotGateway 超时未响应时：

- 标记连接不可用。
- 关闭 writer。
- 清空或失败当前 pending request。
- 进入指数退避重连。

### 6.2 重连

重连策略：

- 初始延迟，例如 1 秒。
- 指数退避。
- 最大延迟，例如 60 秒。
- 重连成功后重新握手。
- 重新发送未 ACK 的 outbox 结果。

### 6.3 ACK 与 outbox

发送 `DecisionResult` 后必须等待 `DecisionAck`。

如果在 ACK 前断线：

- 将 `DecisionResult` 以 `decision_id` 为幂等键写入 outbox。
- 重连后补发。
- RobotGateway 也应按 `decision_id` 幂等处理，避免重复执行动作。

建议新增：

```text
src/core/infrastructure/decision_outbox.py
```

或使用 Redis stream / PostgreSQL 表实现。

### 6.4 并发控制

每 5 分钟轮询与 LangGraph 耗时存在冲突风险。当前 `run_agent_task` 内部有 300 秒超时，刚好等于目标轮询间隔。

建议：

- 默认 single-flight：同一连接同一决策域只允许一个决策循环。
- 如果上一次未完成，下一次 tick 记录 skipped，不并发执行。
- 也可以改为“上一次完成后等待 300 秒再请求下一次”，但这会使实际请求间隔变为 `决策耗时 + 300 秒`。

## 7. 测试方案

### 7.1 新增单元测试

建议新增：

```text
tests/unit/test_robotgateway_tcp_codec.py
tests/unit/test_robotgateway_tcp_client.py
tests/unit/test_robotgateway_tcp_messages.py
tests/unit/test_decision_service.py
tests/unit/test_single_action_output.py
```

覆盖：

- protobuf round-trip。
- 4 字节长度帧编码和解码。
- 半包、多包、超长包。
- decode error。
- request_id 关联。
- ACK 成功和超时。
- 断线重连。
- outbox 补发。
- 单动作输出校验。

### 7.2 替换旧 RobotGateway HTTP 测试

待删除或重写：

```text
tests/unit/test_robotgateway_http_source.py
tests/unit/test_robotgateway_callback.py
tests/unit/test_analysis_flow_callback.py
```

这些测试验证的是 HTTP GET / POST 逻辑，目标架构下不应继续作为主链路测试。

### 7.3 新增集成测试

建议新增 fake TCP RobotGateway：

```text
tests/mocks/robotgateway_tcp_server.py
tests/integration/test_robotgateway_tcp_e2e.py
```

覆盖完整流程：

```text
fake TCP RobotGateway
  <- ClientHello
  -> ServerHello
  <- GameStateRequest
  -> GameStateResponse
  <- DecisionResult
  -> DecisionAck
```

断言：

- AI 决策层主动连接。
- 发送的是 protobuf 帧。
- 每次 GameStateResponse 触发 LangGraph 决策。
- 返回结果只有一条推荐动作。
- 返回结果包含原因。

## 8. 分阶段实施顺序

### Phase 1: 协议与代码生成

- [ ] 和 RobotGateway 对齐 `decision.proto`。
- [ ] 新增 proto 文件。
- [ ] 添加 protobuf 依赖和生成脚本。
- [ ] 生成 Python protobuf 模块。
- [ ] 编写 protobuf round-trip 测试。

### Phase 2: TCP 传输层

- [ ] 实现 length-prefixed codec。
- [ ] 实现 `RobotGatewayTcpClient`。
- [ ] 实现握手、心跳、请求响应关联。
- [ ] 实现断线重连。
- [ ] 编写 codec 和 client 单元测试。

### Phase 3: 决策服务抽取

- [ ] 从 Prefect Flow 中抽出 `run_decision_cycle`。
- [ ] 保留 LangGraph 主图复用。
- [ ] 将 snapshot / recent_events 输入标准化。
- [ ] 将存储逻辑迁移到决策服务。
- [ ] 编写决策服务单元测试。

### Phase 4: 单动作输出改造

- [ ] 修改 Pydantic 输出模型。
- [ ] 修改行动推理 prompt。
- [ ] 修改 `merge_output_node`。
- [ ] 修改 `tracking_update_node`。
- [ ] 更新相关测试。

### Phase 5: TCP 常驻运行时

- [ ] 新增 `src/runtime/robotgateway_decision_client.py`。
- [ ] 新增 5 分钟轮询循环。
- [ ] 接入 TCP state provider。
- [ ] 接入 `run_decision_cycle`。
- [ ] 发送 `DecisionResult` 并等待 ACK。
- [ ] 编写 fake TCP Gateway 集成测试。

### Phase 6: 清理 HTTP RobotGateway 主链路

- [ ] 删除或停用 RobotGateway HTTP snapshot 逻辑。
- [ ] 删除或停用 RobotGateway HTTP callback 逻辑。
- [ ] 删除或重写旧 HTTP RobotGateway 测试。
- [ ] 更新 `.env.example`。
- [ ] 更新启动脚本。
- [ ] 更新 `docker-compose.dev.yml`。
- [ ] 更新通信相关文档。

## 9. 需要和 RobotGateway 确认的问题

1. `GameStateRequest` 是请求单个玩家状态，还是 RobotGateway 返回一批待决策对象。
2. `snapshot` 是否允许第一阶段用 protobuf `Struct` 承载动态字段，还是必须全部强类型。
3. RobotGateway 可执行动作枚举是否与当前五类动作一致：
   - `observe_current_state`
   - `move_to_location`
   - `stop_moving`
   - `jump`
   - `play_basic_action`
4. `DecisionResult` 收到后 RobotGateway 是否会立即执行动作，还是只存储推荐结果。
5. ACK 的语义是什么：收到即 ACK，还是执行成功后 ACK。
6. 断线重连后，RobotGateway 是否按 `decision_id` 做幂等去重。
7. 鉴权使用共享 token、证书、还是内网可信连接。
8. 心跳超时时间和重连策略由哪一端主导。
9. 多租户信息由握手确定，还是每个 `GameStateResponse` 都携带。
10. 每 5 分钟周期是严格墙钟周期，还是“上一轮完成后等待 5 分钟”。

## 10. 保留与删除建议

建议保留：

- LangGraph 主图和大部分节点逻辑。
- LightRAG 查询能力。
- LLM provider / balancer。
- PostgreSQL 分析结果存储。
- Redis 基础设施能力。
- 行动追踪和玩家记忆系统。

建议从 RobotGateway 主链路删除或停用：

- HTTP Webhook 触发。
- HTTP RobotGateway snapshot 拉取。
- HTTP RobotGateway callback。
- RobotGateway 相关 HTTP mock server。
- 旧 callback URL / snapshot base URL 配置。

可选保留：

- FastAPI 管理接口，例如租户、provider、quota、结果查询。

如果要求“整个项目彻底不提供 HTTP”，则 FastAPI 管理接口也应进入后续移除范围；如果要求只是“AI 决策层与 RobotGateway 之间不再使用 HTTP”，则 FastAPI 可以作为内部管理面保留，但不参与 RobotGateway 主链路。
