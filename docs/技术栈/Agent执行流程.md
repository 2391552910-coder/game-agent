# Agent执行流程

> **Workspace:** d:\Projects\myAgent2


当前项目的 agent 执行流程可以分成两层看：**外层触发调度流程** 和 **内层 LangGraph Agent 图执行流程**。

---

## 1. 总体链路

整体是：

```text
游戏服务器 Webhook
  → FastAPI 鉴权/限流
  → /webhooks/player-event
  → offline 事件触发离线分析调度
  → Redis 防抖去重
  → Prefect analysis_flow
  → run_agent_task
  → LangGraph graph.ainvoke(initial_state)
  → 多节点线性执行
  → final_output
  → 写入 analysis_results
  → 回调 RobotGateway
```

对应入口主要在：

- FastAPI 应用入口：[main.py](file:///d:/Projects/myAgent2/src/api/main.py#L1-L70)
- Webhook 路由：[webhooks.py](file:///d:/Projects/myAgent2/src/api/routes/webhooks.py#L24-L58)
- 调度触发器：[triggers.py](file:///d:/Projects/myAgent2/src/core/scheduler/triggers.py#L24-L75)
- Prefect Flow：[analysis_flow.py](file:///d:/Projects/myAgent2/src/core/scheduler/flows/analysis_flow.py#L106-L148)
- LangGraph 图构建：[orchestrator.py](file:///d:/Projects/myAgent2/src/core/agents/orchestrator.py#L31-L58)

---

## 2. 外层：从 Webhook 到 Prefect Flow

### 2.1 FastAPI 启动时初始化基础设施

项目启动时会初始化：

- PostgreSQL 连接
- Redis 连接
- LLM Provider 负载均衡器

代码在 [main.py](file:///d:/Projects/myAgent2/src/api/main.py#L18-L34)。

同时注册了两个中间件：

```text
RateLimitMiddleware
AuthMiddleware
```

也就是每个非公开接口都会先走：

1. API Key 鉴权
2. Redis 滑动窗口限流

鉴权逻辑在 [middleware.py](file:///d:/Projects/myAgent2/src/api/middleware.py#L1-L75)。

---

### 2.2 Webhook 接收玩家事件

核心接口是：

```http
POST /webhooks/player-event
```

代码在 [webhooks.py](file:///d:/Projects/myAgent2/src/api/routes/webhooks.py#L24-L58)。

它根据 `event_type` 分三类：

```text
offline              → 调度离线分析
online               → 取消离线分析
behavior_checkpoint  → 记录在线行为事件
```

其中真正触发 agent 的是 `offline`：

```python
run_id = await schedule_offline_analysis(
    user_id=event.user_id,
    tenant_id=tenant_id,
    snapshot=event.snapshot,
)
```

也就是说：**玩家离线后，才会触发一次分析 agent。**

---

### 2.3 Redis 防抖去重

`offline` 事件进入 [schedule_offline_analysis](file:///d:/Projects/myAgent2/src/core/scheduler/triggers.py#L24-L75)。

这里用 Redis 做防抖：

```text
debounce:{user_id}
```

流程是：

```text
SET debounce:{user_id} pending-xxx EX TTL NX
```

含义是：

- 如果 key 不存在，说明当前没有同一玩家的分析任务，允许调度
- 如果 key 已存在，说明已经有任务在等待或执行，直接忽略
- TTL 来自 `settings.offline_trigger_minutes * 60`

所以同一玩家短时间多次 offline，不会重复触发多次 agent。

提交成功后，会调用 Prefect：

```python
flow_run = await run_deployment(
    name="analysis_flow/offline-analysis",
    parameters={"user_id": user_id, "tenant_id": tenant_id, "snapshot": snapshot},
    timeout=0,
)
```

这里 `timeout=0` 表示不等待 Flow 完成，直接返回。

---

### 2.4 Prefect Flow 执行

Prefect 主流程在 [analysis_flow.py](file:///d:/Projects/myAgent2/src/core/scheduler/flows/analysis_flow.py#L106-L148)。

流程结构是：

```text
analysis_flow
  → 如果 webhook 带了 snapshot，直接用
  → 如果没有 snapshot，调用 fetch_snapshot_task 拉取
  → run_agent_task 执行 LangGraph Agent
  → store_result_task 保存分析结果
  → send_callback_task 回调 RobotGateway
```

关键 agent 执行入口是 [run_agent_task](file:///d:/Projects/myAgent2/src/core/scheduler/flows/analysis_flow.py#L39-L75)：

```python
graph = build_orchestrator().compile()
result = await asyncio.wait_for(
    graph.ainvoke(initial_state),
    timeout=300,
)
```

这里说明：

- 当前 Flow 里用的是 `build_orchestrator().compile()`
- 没有使用 checkpointer
- 超时时间是 300 秒
- 初始 State 由 Flow 手动构造

---

## 3. 内层：LangGraph Agent 图执行流程

LangGraph 图定义在 [orchestrator.py](file:///d:/Projects/myAgent2/src/core/agents/orchestrator.py#L31-L58)。

当前图是一个**线性 StateGraph**：

```text
START
  → fetch_snapshot
  → retrieve_rag_context
  → intent_inference
  → goal_evaluation
  → gather_context
  → behavior_analysis
  → action_reasoning
  → merge_output
  → tracking_update
  → memory_update
  → END
```

代码中逐个注册节点：

```python
builder.add_node("fetch_snapshot", fetch_snapshot_node)
builder.add_node("retrieve_rag_context", retrieve_rag_context_node)
builder.add_node("intent_inference", intent_inference_node)
builder.add_node("goal_evaluation", goal_evaluation_node)
builder.add_node("gather_context", gather_context_node)
builder.add_node("behavior_analysis", behavior_analysis_node)
builder.add_node("action_reasoning", action_reasoning_node)
builder.add_node("merge_output", merge_output_node)
builder.add_node("tracking_update", tracking_update_node)
builder.add_node("memory_update", memory_update_node)
```

然后通过 `add_edge` 串成线性流程。

---

## 4. State 是怎么传的

State 类型定义在 [state.py](file:///d:/Projects/myAgent2/src/core/agents/state.py#L1-L47)。

核心字段包括：

```text
user_id
tenant_id
snapshot
rag_context
enriched_context
behavior_report
reasoned_actions
final_output
errors
tracking_summary
anomalies
abandoned_tracking_ids
intent_result
goal_evaluation_result
player_memory
```

其中几个列表字段用了 reducer：

```python
reasoned_actions: Annotated[list[dict], operator.add]
errors: Annotated[list[str], operator.add]
anomalies: Annotated[list[str], operator.add]
abandoned_tracking_ids: Annotated[list[str], operator.add]
```

这表示这些字段不是覆盖，而是追加合并。

---

## 5. 每个节点具体做什么

### 5.1 fetch_snapshot

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L43-L57)

职责很轻：

```text
验证 snapshot 是否存在
```

当前项目中 snapshot 是上游 Prefect Flow 注入的，节点本身不主动拉取游戏数据库。

如果没有 snapshot，会写入：

```python
{"errors": ["snapshot为空，无法分析"]}
```

---

### 5.2 retrieve_rag_context

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L61-L94)

作用：

```text
从 snapshot 提取文本字段
构造 RAG query
调用 LightRAG hybrid 检索
写入 rag_context
```

调用的是：

```python
rag = await get_rag()
context = await rag.aquery(query, param=QueryParam(mode="hybrid"))
```

查询构造逻辑在 [nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L97-L125)。

它只抽取 snapshot 里的字符串值，比如：

```json
{
  "current_area": "商业区",
  "profession": "程序员",
  "recent_activities": ["购物", "健身"]
}
```

会拼成类似：

```text
商业区 程序员 购物 健身
```

这个设计避免把字段名、数字、ID 直接带入 RAG。

---

### 5.3 intent_inference

位置：[decision_nodes.py](file:///d:/Projects/myAgent2/src/core/agents/decision_nodes.py#L34-L95)

作用：

```text
读取最近一次 session_events
读取最近 3 条历史 player_intent
结合 player_memory
用 fast 模型推断本次会话意图
写入 intent_result
```

它会查：

- `session_events`
- `player_intent`
- `player_memory`，但注意当前 Flow 初始 State 没有注入 `player_memory`，所以多数情况下这里是空记忆

模型调用方式：

```python
llm = await get_llm(model_type="fast")
llm_structured = llm.with_structured_output(InferredIntent, method="function_calling")
```

也就是说这个节点要求 LLM 输出结构化的 `InferredIntent`。

---

### 5.4 goal_evaluation

位置：[decision_nodes.py](file:///d:/Projects/myAgent2/src/core/agents/decision_nodes.py#L100-L165)

作用：

```text
读取最近一个 active 目标
结合当前 snapshot、intent_result、player_memory
判断目标是继续、降级、切换，还是创建新目标
写入 goal_evaluation_result
```

它查的是 `player_intent` 表中最近一条 active 目标：

```sql
WHERE goal_status = 'active'
ORDER BY created_at DESC
LIMIT 1
```

模型使用 `default` 主力模型：

```python
llm = await get_llm(model_type="default")
llm_structured = llm.with_structured_output(GoalEvaluationResult, method="function_calling")
```

输出写入：

```python
{"goal_evaluation_result": evaluation.model_dump()}
```

---

### 5.5 gather_context

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L128-L236)

这是当前 agent 中比较关键的一个节点。

作用：

```text
让 LLM 自主决定调用哪些工具
工具返回结果追加到 enriched_context
同时抽取 tracking_summary、anomalies、abandoned_tracking_ids
```

它使用 fast 模型：

```python
llm = await get_llm(model_type="fast")
llm_with_tools = llm.bind_tools(tools)
```

工具来自 [tools.py](file:///d:/Projects/myAgent2/src/core/agents/tools.py#L383-L467)，一共 5 个：

```text
query_player_history
query_similar_players
dynamic_rag_query
get_action_tracking
detect_anomaly
```

这个节点内部不是简单调用一次工具，而是一个小循环：

```text
最多 3 轮 LLM 工具选择
最多 8 次总工具调用
每次外部调用 60 秒超时
```

也就是：

```text
LLM 判断需要哪些工具
  → 执行工具
  → 工具结果作为 ToolMessage 回给 LLM
  → LLM 再判断是否继续调用
  → 最多 3 轮
```

其中：

- `get_action_tracking` 的结果会写入 `tracking_summary`
- `detect_anomaly` 的结果会写入 `anomalies`
- 如果 action tracking 里发现目标冲突，会提取 `CONFLICT_IDS` 写入 `abandoned_tracking_ids`

---

### 5.6 behavior_analysis

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L240-L281)

作用：

```text
基于 snapshot + rag_context + enriched_context
用 fast 模型生成玩家行为画像
写入 behavior_report
```

输出模型是 [BehaviorProfile](file:///d:/Projects/myAgent2/src/core/agents/models.py#L14-L20)：

```python
class BehaviorProfile(BaseModel):
    playstyle: str
    current_goal: list[str]
    bottlenecks: list[str]
    engagement_level: Literal["high", "medium", "low"]
```

调用方式：

```python
llm = await get_llm(model_type="fast")
llm = llm.with_structured_output(BehaviorProfile, method="function_calling")
```

最终写入 State：

```python
{"behavior_report": profile.model_dump_json()}
```

注意这里保存的是 JSON 字符串。

---

### 5.7 action_reasoning

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L285-L350)

作用：

```text
基于行为画像、RAG、工具上下文、追踪摘要、异常、意图推断、目标校验
用 default 主力模型生成推荐行动列表
写入 reasoned_actions
```

它读取的信息很多：

```text
snapshot
rag_context
enriched_context
behavior_report
tracking_summary
anomalies
intent_result
goal_evaluation_result
```

输出模型是 [ActionList](file:///d:/Projects/myAgent2/src/core/agents/models.py#L64-L69)，里面包含多个 [RecommendedAction](file:///d:/Projects/myAgent2/src/core/agents/models.py#L22-L60)。

当前允许的动作类型只有这几类：

```python
ActionType = Literal[
    "observe_current_state",
    "move_to_location",
    "stop_moving",
    "jump",
    "play_basic_action",
]
```

所以这个 agent 最终不是直接输出任意自然语言建议，而是输出受约束的可执行动作。

---

### 5.8 merge_output

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L354-L381)

作用：

```text
把 behavior_report 和 reasoned_actions 合并成最终 PlayerAnalysisOutput
写入 final_output
```

最终结构是 [PlayerAnalysisOutput](file:///d:/Projects/myAgent2/src/core/agents/models.py#L59-L61)：

```python
class PlayerAnalysisOutput(BaseModel):
    player_profile: BehaviorProfile
    recommended_actions: list[RecommendedAction]
```

也就是最终输出长这样：

```json
{
  "player_profile": {
    "playstyle": "...",
    "current_goal": [],
    "bottlenecks": [],
    "engagement_level": "medium"
  },
  "recommended_actions": [
    {
      "action_type": "move_to_location",
      "priority": "high",
      "reason": "...",
      "payload": {},
      "goal_metric": "...",
      "goal_value": 10,
      "expected_hours": 24
    }
  ]
}
```

---

### 5.9 tracking_update

位置：[nodes.py](file:///d:/Projects/myAgent2/src/core/agents/nodes.py#L385-L527)

这是监督机制节点。

作用分两步：

#### 第一步：更新旧追踪记录

它查询 `action_tracking` 表中状态为 `tracking` 的旧行动：

```sql
WHERE user_id = :user_id
  AND tenant_id = :tenant_id
  AND status = 'tracking'
```

然后判断：

```text
如果在 abandoned_tracking_ids 里 → abandoned
如果当前 snapshot 指标达到 goal_value → completed
如果超过 deadline → timeout
```

#### 第二步：写入本次新的可追踪行动

它从 `final_output.recommended_actions` 里筛选有 `goal_metric` 的 action：

```python
trackable = [a for a in actions if a.get("goal_metric")]
```

然后写入新的 `action_tracking` 记录。

所以不是所有推荐动作都会被追踪，只有带明确量化目标的行动才会进入监督机制。

---

### 5.10 memory_update

位置：[decision_nodes.py](file:///d:/Projects/myAgent2/src/core/agents/decision_nodes.py#L170-L204)

这是长期记忆更新节点。

作用：

```text
upsert player_memory
insert player_intent
```

具体做两件事：

1. 更新玩家长期行为画像和目标历史
2. 保存本次意图推断和目标决策结果

保存意图记录逻辑在 [decision_nodes.py](file:///d:/Projects/myAgent2/src/core/agents/decision_nodes.py#L482-L536)。

它会把本次：

```text
intent_result
goal_evaluation_result
current_goal
goal_type
goal_status
goal_progress
decision
decision_reason
```

写入 `player_intent` 表。

---

## 6. LLM 是怎么选模型的

所有节点调用 LLM 都通过 [factory.py](file:///d:/Projects/myAgent2/src/core/llm/factory.py#L18-L54) 的：

```python
get_llm(model_type="fast")
get_llm(model_type="default")
```

逻辑是：

```text
优先从 DB provider pool 负载均衡选择
如果没有可用 provider
  → 回退到 .env 里的 OpenAI-compatible 配置
```

所以项目里分两类模型：

```text
fast     → 快速任务，比如意图推断、上下文收集、行为分析
default  → 深度推理，比如目标校验、行动推理
```

---

## 7. 最终结果如何落库和回调

Agent 执行完成后，Prefect Flow 从结果里取：

```python
output = result.get("final_output", {})
```

然后调用：

```python
await store_result_task(...)
await send_callback_task(...)
```

结果保存逻辑在 [result_store.py](file:///d:/Projects/myAgent2/src/core/infrastructure/result_store.py#L17-L51)。

它会写入：

```text
analysis_results
```

字段包括：

```text
tenant_id
user_id
snapshot_hash
output_json
analyzed_at
```

然后再通过 `send_callback_task` 回调 RobotGateway。

---

## 8. 当前项目 agent 的核心特点

### 8.1 不是 ReAct Agent，而是 LangGraph 线性编排

它不是一个完全自由循环的 ReAct Agent，而是：

```text
固定 LangGraph 流程 + 局部工具调用循环
```

只有 `gather_context` 节点内部有 LLM 工具调用循环。

主图本身没有条件分支，所有节点都会按顺序执行。

---

### 8.2 RAG 是两层

第一层是主图统一 RAG：

```text
retrieve_rag_context
```

根据 snapshot 自动查一次知识库。

第二层是工具动态 RAG：

```text
dynamic_rag_query
```

在 `gather_context` 里由 LLM 判断是否需要进一步查某个具体主题。

---

### 8.3 输出是强约束结构化输出

行为画像和推荐动作都不是裸文本，而是 Pydantic 结构化模型：

- `BehaviorProfile`
- `ActionList`
- `RecommendedAction`
- `PlayerAnalysisOutput`

相关定义在 [models.py](file:///d:/Projects/myAgent2/src/core/agents/models.py#L1-L69)。

---

### 8.4 有监督闭环

当前 agent 不只是“分析一次输出建议”，它还有闭环：

```text
本次输出带 goal_metric 的 action
  → tracking_update 写入 action_tracking
下一次分析
  → get_action_tracking 查询上次行动完成情况
  → action_reasoning 根据完成/超时/放弃状态调整策略
```

这就是项目里的“监督机制”。

---

### 8.5 有长期记忆和目标延续

动态决策系统通过：

```text
session_events
player_intent
player_memory
```

支持：

```text
在线期间行为事件积累
离线后推断本次意图
对比历史目标
判断继续/降级/切换/新目标
更新长期记忆
```

不过有一个需要注意的点：当前 `run_agent_task` 初始化 State 时没有加载 `player_memory` 字段，只传了：

```python
"user_id"
"tenant_id"
"snapshot"
"rag_context"
"enriched_context"
"behavior_report"
"reasoned_actions"
"final_output"
"errors"
"tracking_summary"
"anomalies"
"abandoned_tracking_ids"
```

也就是说，虽然 `AnalysisState` 里定义了 `player_memory`，`intent_inference` 和 `goal_evaluation` 也会读取它，但当前主链路里似乎没有在图执行前把 DB 里的 `player_memory` 加载进来。这可能会导致“长期记忆”在推理时没有真正参与，只在最后 `memory_update` 写入。这个点值得后续代码审查重点关注。

---

## 9. 一句话总结

当前项目的 agent 是：

```text
FastAPI 接收玩家离线事件，
Redis 防抖后提交 Prefect Flow，
Flow 构造 LangGraph State 并调用 graph.ainvoke，
LangGraph 按固定线性节点依次完成：
快照校验 → RAG 检索 → 意图推断 → 目标校验 → 工具补充上下文 → 行为画像 → 行动推理 → 输出合并 → 行动追踪更新 → 长期记忆更新，
最后把 final_output 存库并回调 RobotGateway。
```

核心实现入口是 [orchestrator.py](file:///d:/Projects/myAgent2/src/core/agents/orchestrator.py#L31-L58)。

