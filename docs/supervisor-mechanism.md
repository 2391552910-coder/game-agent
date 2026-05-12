# 监督机制实现分析

本文档记录 myAgent v2.0 监督机制的完整实现过程，包括设计思路、代码改动细节和运行逻辑。

---

## 一、背景与目标

### 原有架构的局限

原有 Agent 每次分析都是无状态的：只看当前快照，不知道上次推荐了什么，不知道玩家有没有执行，也无法感知异常情况。

```
原有流程（无状态）：
玩家下线 → 分析当前快照 → 输出推荐行动 → 结束
下次下线 → 重新分析当前快照 → 输出新推荐 → 结束（不知道上次推荐的结果）
```

### 监督机制目标

在 Agent 内部引入状态追踪能力，使每次分析能够：

1. **感知上次行动完成情况**：上次推荐的行动做完了没有，进度如何
2. **检测突发异常**：流失风险、活跃度骤降、重复卡关等
3. **给出针对性的下一步反馈**：基于完成情况和异常，调整推荐策略

关键设计原则：**监督机制是 Agent 的扩展，不是独立模块**。所有逻辑在 LangGraph 图内部完成，通过新增工具和节点实现，不改变外部接口。

---

## 二、架构变化

### 扩展后的 Agent 图

```
原有图（6节点）：
START → fetch_snapshot → retrieve_rag_context → gather_context
      → behavior_analysis → action_reasoning → merge_output → END

扩展后（7节点）：
START → fetch_snapshot → retrieve_rag_context → gather_context（+2个新工具）
      → behavior_analysis → action_reasoning（+追踪上下文输入）
      → merge_output → tracking_update（新节点）→ END
```

### 数据流变化

```
gather_context 阶段：
  LLM 调用 get_action_tracking → 查询 action_tracking 表 → 返回上次行动进度
  LLM 调用 detect_anomaly      → 规则检测异常 → 返回异常列表
  结果写入 State: tracking_summary, anomalies

action_reasoning 阶段：
  读取 tracking_summary + anomalies → LLM 感知历史 → 调整推荐策略
  输出 RecommendedAction（含可选追踪字段 goal_metric/goal_value/expected_hours）

tracking_update 阶段（新增）：
  读取旧追踪记录 → 对比当前快照指标 → 更新状态（completed/timeout）
  读取本次推荐行动 → 写入新追踪记录（仅有 goal_metric 的行动）
```

---

## 三、文件改动详情

### 1. `alembic/versions/004_action_tracking.py`（新建）

新增 `action_tracking` 数据库表，存储每次分析推荐的可追踪行动。

**表结构：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | UUID | 主键，自动生成 |
| `tenant_id` | UUID | 租户 ID，多租户隔离 |
| `user_id` | VARCHAR | 玩家 ID |
| `analysis_id` | UUID | 关联的分析记录（可选） |
| `action_type` | VARCHAR | 行动类型，如 `complete_course` |
| `action_desc` | TEXT | 行动描述，来自 `reason` 字段 |
| `goal_metric` | VARCHAR | 完成判断指标，对应快照字段名 |
| `goal_value` | FLOAT | 目标值，达到此值视为完成 |
| `baseline_value` | FLOAT | 推荐时的基准值，用于计算进度 |
| `expected_hours` | INTEGER | 预计完成小时数 |
| `deadline` | TIMESTAMPTZ | 截止时间，由 `expected_hours` 计算 |
| `status` | VARCHAR | `tracking` / `completed` / `timeout` / `abandoned` |
| `completed_at` | TIMESTAMPTZ | 完成时间 |
| `completion_snapshot` | JSON | 完成时的关键指标快照 |
| `created_at` | TIMESTAMPTZ | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 最后更新时间 |

**索引：**
- `ix_action_tracking_tenant_user`：按租户+用户查询（最常用）
- `ix_action_tracking_status`：按状态筛选进行中的追踪
- `ix_action_tracking_created_at`：按时间排序

**执行迁移：**
```bash
alembic upgrade head
```

---

### 2. `src/core/agents/models.py`

`RecommendedAction` 新增三个可选追踪字段：

```python
class RecommendedAction(BaseModel):
    # 原有字段
    action_type: str
    priority: Literal["high", "medium", "low"]
    reason: str
    payload: dict

    # 新增追踪字段（可选，LLM 在能量化时填写）
    goal_metric: str | None    # 快照中的数值字段名，如 "learning_courses"
    goal_value: float | None   # 目标值，达到此值视为完成
    expected_hours: int | None # 预计完成小时数，用于计算截止时间
```

**设计原则：**
- 三个字段均为可选，LLM 只在行动有明确可量化目标时填写
- `goal_metric` 必须是快照中实际存在的数值字段
- `tracking_update_node` 只处理有 `goal_metric` 的行动，无法量化的行动不追踪

---

### 3. `src/core/agents/state.py`

`AnalysisState` 新增两个监督机制专用字段：

```python
class AnalysisState(TypedDict):
    # 原有字段...

    # 监督机制字段
    tracking_summary: str                        # get_action_tracking 工具写入
    anomalies: Annotated[list[str], operator.add] # detect_anomaly 工具写入
```

**字段说明：**
- `tracking_summary`：可读文本，包含上次行动的完成状态摘要和进度详情
- `anomalies`：使用 `operator.add` reducer，支持多次追加，每条为一个异常描述

---

### 4. `src/core/agents/tools.py`

新增两个监督机制工具，并修改 `create_tools()` 签名以接收 `snapshot` 参数。

#### 4.1 `create_tools()` 签名变更

```python
# 原来
def create_tools(tenant_id: str, user_id: str) -> list:

# 现在
def create_tools(tenant_id: str, user_id: str, snapshot: dict | None = None) -> list:
```

`snapshot` 通过闭包注入两个新工具，用于实时计算完成状态和检测异常。

#### 4.2 新工具：`get_action_tracking`

查询 `action_tracking` 表中 `status='tracking'` 的记录，结合当前快照实时计算完成状态。

**完成判断逻辑（优先级顺序）：**

```
1. 指标对比（主要）：
   current_val = snapshot[goal_metric]（支持顶层字段和 stats 嵌套字段）
   if current_val >= goal_value → completed

2. 截止时间（兜底）：
   if now > deadline → timeout

3. 其他情况 → tracking（继续进行中）
```

**返回格式示例：**
```
追踪行动共 3 条：已完成 1 / 超时 1 / 进行中 1

[
  {
    "action_type": "complete_course",
    "action_desc": "完成3个编程课程提升技能",
    "status": "completed",
    "progress": "learning_courses: 8 → 11 / 目标 11 (100%)"
  },
  ...
]
```

#### 4.3 新工具：`detect_anomaly`

基于规则检测异常，不调用 LLM，速度快且结果确定。

**检测规则：**

| 规则 | 判断条件 | 示例输出 |
|------|---------|---------|
| 行动超时 | `action_tracking` 中有超过 `deadline` 的进行中记录 | `行动超时: 2 条追踪行动已超过截止时间未完成` |
| 重复卡关 | 当前快照 `bottlenecks` 与上次分析结果完全相同 | `重复卡关: 瓶颈与上次分析完全相同 — 缺乏社交互动, 资金不足` |

> 注：流失风险和活跃度骤降规则依赖历史快照中的 `play_hours` 数据，但 `analysis_results` 表只存储分析输出（`PlayerAnalysisOutput`），不存储原始快照，因此这两条规则无法可靠实现，已移除。

#### 4.4 辅助函数：`_extract_metric()`

从快照中提取数值指标，支持两种结构：

```python
# 顶层字段
snapshot = {"learning_courses": 11}
_extract_metric(snapshot, "learning_courses")  # → 11.0

# stats 嵌套字段
snapshot = {"stats": {"play_hours": 180}}
_extract_metric(snapshot, "play_hours")  # → 180.0
```

---

### 5. `src/core/agents/nodes.py`

#### 5.1 `gather_context_node` 更新

两处改动：

**改动1：传入 snapshot 给工具**
```python
# 原来
tools = create_tools(state["tenant_id"], state["user_id"])

# 现在
tools = create_tools(state["tenant_id"], state["user_id"], state.get("snapshot"))
```

**改动2：监督工具结果写入专用 State 字段**

工具执行后，除了追加到 `enriched_parts`，还额外提取到专用字段：

```python
if tool_name == "get_action_tracking":
    tracking_summary = str(result)
elif tool_name == "detect_anomaly" and str(result) != "无异常":
    anomaly_lines = [line for line in str(result).splitlines() if line.strip()]
    anomalies_found.extend(anomaly_lines)

# 最终返回时写入 State
result["tracking_summary"] = tracking_summary
result["anomalies"] = anomalies_found
```

这样 `action_reasoning` 节点可以直接从 State 读取，不需要解析 `enriched_context` 字符串。

#### 5.2 `action_reasoning_node` 更新

新增读取 `tracking_summary` 和 `anomalies`，传入 prompt：

```python
tracking_summary = state.get("tracking_summary", "") or "（无行动追踪记录，首次分析）"
anomalies = state.get("anomalies", [])
anomaly_text = "\n".join(anomalies) if anomalies else "（无异常）"

chain.ainvoke({
    ...
    "tracking_summary": tracking_summary,
    "anomaly_text": anomaly_text,
})
```

#### 5.3 新增 `tracking_update_node`（节点7）

图中最后一个节点，在 `merge_output` 之后执行。

**职责1：更新旧追踪记录状态**

查询所有 `status='tracking'` 的记录，对比当前快照：
- 指标达标 → `UPDATE status='completed', completed_at=now, completion_snapshot={...}`
- 超过截止时间 → `UPDATE status='timeout'`

**职责2：写入本次新追踪记录**

遍历 `final_output.recommended_actions`，只处理有 `goal_metric` 的行动：
- 从快照提取 `baseline_value`（当前值作为基准）
- 根据 `expected_hours` 计算 `deadline`
- `INSERT INTO action_tracking ...`

**事务隔离：** 步骤1和步骤2使用独立事务。步骤2的 INSERT 失败不会回滚步骤1已提交的 UPDATE，两者互不影响。追踪失败只记录错误，不影响主流程返回结果。

---

### 6. `src/core/agents/orchestrator.py`

注册新节点并添加边：

```python
builder.add_node("tracking_update", tracking_update_node)
builder.add_edge("merge_output", "tracking_update")
builder.add_edge("tracking_update", END)
```

---

### 7. `src/core/agents/prompts.py`

#### 7.1 `CONTEXT_GATHERING_SYSTEM` 更新

新增两个工具的说明，并明确要求每次必须调用：

```
- get_action_tracking: 查询上次推荐行动的完成情况（监督机制，必须调用）
- detect_anomaly: 检测当前是否存在异常情况（监督机制，必须调用）

关键原则：
1. 每次分析必须调用 get_action_tracking 和 detect_anomaly
```

#### 7.2 `ACTION_REASONING_SYSTEM` 更新

新增监督机制推理规则：

```
监督机制要求：
- 如果上次行动已完成（completed），推荐下一阶段更高难度的目标
- 如果上次行动超时（timeout），推荐降低难度的替代方案，并分析原因
- 如果检测到异常，必须将异常处理行动设为 high 优先级
- 对于可量化完成条件的行动，必须填写 goal_metric、goal_value、expected_hours
```

#### 7.3 `ACTION_REASONING_USER` 更新

新增两个模板变量：

```
上次推荐行动完成情况：
{tracking_summary}

当前异常检测结果：
{anomaly_text}
```

---

### 8. `src/api/routes/webhooks.py`

透传游戏服务器推送的 `snapshot`：

```python
run_id = await schedule_offline_analysis(
    user_id=event.user_id,
    tenant_id=tenant_id,
    snapshot=event.snapshot,  # 新增
)
```

---

### 9. `src/core/scheduler/triggers.py`

`schedule_offline_analysis()` 新增 `snapshot` 参数，传给 Prefect Flow：

```python
async def schedule_offline_analysis(
    user_id: str,
    tenant_id: str,
    snapshot: dict | None = None,  # 新增
) -> str | None:
    ...
    flow_run = await run_deployment(
        name="analysis_flow/offline-analysis",
        parameters={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "snapshot": snapshot,  # 新增
        },
        timeout=0,
    )
```

---

### 10. `src/core/scheduler/flows/analysis_flow.py`

`analysis_flow()` 新增 `snapshot` 参数，优先使用推送的快照：

```python
@flow(...)
async def analysis_flow(
    user_id: str,
    tenant_id: str,
    snapshot: dict | None = None,  # 新增
) -> None:
    if snapshot:
        # 游戏服务器已推送，直接使用
        logger.info("使用游戏服务器推送的快照, user_id=%s", user_id)
    else:
        # 未推送，主动从游戏数据库拉取
        snapshot = await fetch_snapshot_task(user_id=user_id)
    ...
```

同时 `run_agent_task` 的初始 State 新增监督机制字段：

```python
graph.ainvoke({
    ...
    "tracking_summary": "",  # 新增
    "anomalies": [],         # 新增
})
```

---

## 四、完整运行示例

### 第1次分析（无历史）

```
gather_context:
  get_action_tracking → "无进行中的行动追踪记录（首次分析）"
  detect_anomaly      → "无异常"

action_reasoning:
  tracking_summary = "（无行动追踪记录，首次分析）"
  anomaly_text     = "（无异常）"
  → LLM 正常推荐，部分行动填写 goal_metric

tracking_update:
  旧记录：0 条
  新记录：写入 2 条（有 goal_metric 的行动）
  例：learning_courses 目标 11，baseline 8，deadline 72h 后
```

### 第2次分析（7天后，课程已完成）

```
gather_context:
  get_action_tracking → "追踪行动共 2 条：已完成 1 / 超时 1 / 进行中 0
                         learning_courses: 8 → 11 / 目标 11 (100%) [completed]
                         shopping_count: 45 → 46 / 目标 50 (20%) [timeout]"
  detect_anomaly      → "无异常"

action_reasoning:
  感知到课程已完成 → 推荐下一阶段目标（更高难度课程）
  感知到购物超时  → 推荐降低难度的替代方案

tracking_update:
  旧记录：completed → UPDATE status='completed'
          timeout   → UPDATE status='timeout'
  新记录：写入本次推荐的新追踪行动
```

### 第3次分析（异常情况：流失风险）

```
gather_context:
  get_action_tracking → "追踪行动共 1 条：进行中 1"
  detect_anomaly      → "流失风险: 本次游戏时长增量 0.3h，仅为上次增量 4.0h 的 8%"

action_reasoning:
  anomaly_text 不为"无异常"
  → 将流失挽留行动设为 high 优先级
  → 推荐低门槛、高回报的引导行动

tracking_update:
  旧记录：无变化（指标未达标，未超时）
  新记录：写入本次推荐的追踪行动
```

---

## 五、关键设计决策

### 为什么完成判断不用 LLM

行动完成判断是确定性逻辑（数值比较），用规则实现：
- 速度快，不消耗 token
- 结果确定，不会因 LLM 幻觉误判
- 逻辑透明，便于调试

LLM 只负责**解读**完成情况并**调整推荐策略**，判断本身由代码完成。

### 为什么追踪字段是可选的

不是所有行动都能量化完成条件。例如"多与其他玩家互动"无法用单一指标衡量。强制要求 LLM 填写会导致填写不准确的 `goal_metric`，反而影响追踪质量。

可选设计让 LLM 只在有把握时填写，`tracking_update_node` 只处理有 `goal_metric` 的行动。

### 为什么 tracking_update 放在 merge_output 之后

`tracking_update` 需要读取 `final_output`（本次推荐的行动），所以必须在 `merge_output` 之后。同时它的失败不应影响主流程返回结果，放在最后且容错处理符合这个要求。

### snapshot 透传的意义

原来 `fetch_snapshot_task` 需要从游戏数据库（`GAME_DB_DSN`）拉取快照，这要求平台能访问游戏数据库，增加了耦合。

游戏服务器在推送离线事件时附带 `snapshot`，平台直接使用，不需要再拉取。这是更松耦合的集成方式，也为未来的在线监测（`snapshot_update` 事件）打下基础。

---

## 六、后续扩展方向

### 在线监测（snapshot_update 事件）

当前只有离线触发分析。如果游戏服务器能配合，可以在 `webhooks.py` 新增 `snapshot_update` 事件类型：

```python
elif event.event_type == "snapshot_update":
    # 不触发完整 Agent 分析
    # 只更新 Redis 中的最新快照缓存
    # 调用 _get_action_tracking 检查是否有行动已完成
    # 如有完成或异常，可触发即时通知
```

### 行动完成通知

`tracking_update_node` 检测到 `completed` 时，可以通过回调接口通知游戏服务器，触发游戏内奖励或引导。

### 追踪数据分析

`action_tracking` 表积累数据后，可以分析：
- 哪类行动完成率最高/最低
- 不同玩法风格的行动完成模式
- `expected_hours` 的准确性（实际完成时间 vs 预估）
