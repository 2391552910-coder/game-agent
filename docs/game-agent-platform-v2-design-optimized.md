# Game Agent Platform v2 — 优化设计文档

**日期**: 2026-04-02  
**基于**: v2 原始设计文档 + 实现计划评审  
**变更摘要**: 修复架构缺陷、补全空实现、统一数据流、提升生产就绪度

---

## 1. 变更概览

| 类别 | 原方案问题 | 本文修正 |
|------|-----------|---------|
| **数据流** | BehaviorGraph / ReasonerGraph 各自独立做 RAG 检索，两次重复 | 主图统一做一次检索，`rag_context` 注入 State |
| **防抖** | 进程内 `asyncio.TimerHandle`，多实例失效 | 改为 Redis TTL Key + Prefect delayed flow |
| **认证** | 中间件只取 Header，未实际验证租户 | 完整 Redis Cache → PostgreSQL 验证链路 |
| **限流** | `RateLimitMiddleware` 是空实现 | Redis ZSET 滑动窗口落地 |
| **tenant_id 透传** | `result_store` 写 `None`，违反 NOT NULL 约束 | `tenant_id` 作为一等公民贯穿 State → Flow → Store |
| **RAG 持久化** | `working_dir=./rag_storage` 容器重启后丢失 | 挂载命名 Volume，容器无状态 |
| **结构化输出** | LLM 返回裸字符串 + 手动 `json.loads`，脆弱 | 改用 `with_structured_output` |
| **CORS** | `allow_origins=["*"]` + `allow_credentials=True` 浏览器拒绝 | 明确 origin 白名单 |
| **配置** | `alembic.ini` 硬编码密码，与 `env.py` 覆盖逻辑冲突 | `alembic.ini` 使用占位符，单一数据源 |

---

## 2. 架构设计（修订版）

### 2.1 整体数据流

```
游戏服务器 Webhook
        ↓
┌───────────────────────────────────────────────┐
│  FastAPI (API 层)                              │
│  ├── AuthMiddleware                           │
│  │     Redis Cache (TTL 5min) ──→ PG 验证     │
│  ├── RateLimitMiddleware                      │
│  │     Redis ZSET 滑动窗口 (100req/min/IP)    │
│  └── Webhook → 写入 Redis 防抖 Key            │
└────────────────────┬──────────────────────────┘
                     ↓ Redis Key 到期
┌───────────────────────────────────────────────┐
│  Prefect (调度层)                              │
│  ├── Redis Key 过期触发 Flow                  │
│  ├── 并发限制 (ConcurrencyLimit)              │
│  └── Flow: fetch → analyze → store            │
│         ↑ tenant_id 全程透传                  │
└────────────────────┬──────────────────────────┘
                     ↓
┌───────────────────────────────────────────────┐
│  LangGraph (Agent 层)                          │
│                                               │
│  AnalysisState {                              │
│    user_id, tenant_id,                        │
│    snapshot,                                  │
│    rag_context,          ← 主图统一检索        │
│    behavior_report,                           │
│    reasoned_actions,                          │
│    final_output,                              │
│    errors                                     │
│  }                                            │
│                                               │
│  START                                        │
│    → fetch_snapshot                           │
│    → retrieve_rag_context   ← 新增，合并检索  │
│    → behavior_analysis      ← 读 rag_context  │
│    → action_reasoning       ← 读 rag_context  │
│    → merge_output                             │
│  END                                          │
│                                               │
│  Checkpointer: PostgresSaver                  │
│  Store: PostgresStore (跨会话玩家画像)         │
└────────────────────┬──────────────────────────┘
                     ↓
┌───────────────────────────────────────────────┐
│  LightRAG (引擎层)                             │
│  working_dir → 命名 Volume (持久化)            │
│  graph_storage → Neo4j                        │
└───────────────────────────────────────────────┘
```

### 2.2 防抖机制（修订）

原方案用进程内 `asyncio.TimerHandle`，在多进程 / 多实例部署时完全失效。

**新方案：Redis TTL Key + Prefect Delayed Flow**

```
玩家离线事件到达
  → SET user:{id}:offline_pending 1 EX {OFFLINE_TRIGGER_MINUTES * 60} NX
  → 若 SET 成功：向 Prefect 提交 delayed flow（延迟同等时间）
  → 若 SET 失败（Key 已存在）：忽略，现有 flow 已在等待

玩家重新上线事件到达
  → DEL user:{id}:offline_pending
  → 取消对应的 Prefect flow run（通过 flow run id，存在 Key value 里）
```

这套方案在任意数量实例间天然共享状态，Redis Key 就是分布式锁。

### 2.3 认证链路（修订）

原方案 `AuthMiddleware` 只读取了 Header，没有验证。

**完整链路：**

```
请求携带 X-API-Key
  → Redis GET auth_cache:{api_key}
      命中 → 取出 tenant_id，注入 request.state，放行
      未命中
        → PostgreSQL: SELECT tenant_id, is_active, quota_used, quota_limit
                       FROM tenants JOIN quotas WHERE api_key = ?
          → 验证通过：写入 Redis SETEX auth_cache:{api_key} 300 {tenant_json}
          → 验证失败：返回 401
          → 配额超限：返回 429
```

`tenant_id` 在此步写入 `request.state.tenant_id`，后续路由和 Prefect Flow 均从此读取，不再有 `None`。

---

## 3. 关键组件修订

### 3.1 LangGraph State（修订）

在 `AnalysisState` 中增加 `rag_context` 字段，消除子图重复检索。

```
AnalysisState
  ├── user_id: str
  ├── tenant_id: str              ← 已有，确保非空
  ├── snapshot: PlayerSnapshot
  ├── rag_context: str            ← 新增，主图统一填充
  ├── behavior_report: str
  ├── reasoned_actions: list[RecommendedAction]   ← 类型化，非裸 dict
  ├── final_output: dict
  └── errors: list[str]
```

**图结构变更：**

```
原：fetch_snapshot → behavior_analysis(内部RAG) → action_reasoning(内部RAG) → merge
新：fetch_snapshot → retrieve_rag_context → behavior_analysis → action_reasoning → merge
                          ↑
                    一次检索，两个节点共享
```

`BehaviorState` 和 `ReasonerState` 中的 `rag_context` 字段不再由子图自行填充，改为从主图注入。

### 3.2 结构化输出（修订）

原方案 LLM 返回裸字符串，`merge_output_node` 手动 `json.loads`，LLM 格式稍有偏差就抛出异常。

**新方案：`with_structured_output`**

- `analyze_behavior` 返回 `BehaviorProfile` Pydantic 模型，而非 JSON 字符串
- `reason_node` 返回 `list[RecommendedAction]` Pydantic 列表，而非裸字符串
- `merge_output_node` 直接组装已验证的模型，不再需要 try/except JSON 解析

```
Pydantic 模型层级：
  BehaviorProfile        ← behavior_graph 输出
  RecommendedAction      ← reasoner_graph 输出（列表）
  PlayerAnalysisOutput   ← merge_output_node 最终输出
    ├── player_profile: BehaviorProfile
    └── recommended_actions: list[RecommendedAction]
```

### 3.3 Prefect Flow（修订）

原方案 `run_analysis` task 内 `tenant_id` 为空字符串。

**修订要点：**

- Flow 入参增加 `tenant_id: str`
- `fetch_player_data` → `run_analysis` → `store_result` 全程携带 `tenant_id`
- `store_result` task 写库时 `tenant_id` 有值，外键约束不再失败
- 并发控制改用 Prefect 原生 `ConcurrencyLimit`，而非 `settings.MAX_CONCURRENT_ANALYSES` 靠应用层自控

```
player_offline_analysis_flow(user_id: str, tenant_id: str)
  → fetch_player_data(user_id)  → snapshot
  → run_analysis(user_id, tenant_id, snapshot)  → result
  → store_result(user_id, tenant_id, result)
```

---

## 4. 基础设施修订

### 4.1 LightRAG 持久化

`working_dir` 挂载命名 Volume，否则容器重启后本地缓存丢失，每次冷启动需要重新从 Neo4j 重建索引。

在 `docker-compose.yml` 中增加：

```yaml
app:
  volumes:
    - rag_storage:/app/rag_storage    ← 新增

volumes:
  rag_storage:
    driver: local
```

`settings.rag_working_dir` 对应容器内路径 `/app/rag_storage`。

### 4.2 Alembic 配置

`alembic.ini` 中 `sqlalchemy.url` 改为占位符，避免与 `env.py` 的覆盖逻辑产生混淆：

```ini
# alembic.ini
sqlalchemy.url = postgresql://placeholder/placeholder
# 实际 URL 由 alembic/env.py 从 settings 读取并覆盖
```

### 4.3 CORS

```python
# 原（错误）
allow_origins=["*"], allow_credentials=True

# 修订
allow_origins=settings.cors_allowed_origins,  # 从 .env 读取，如 ["https://yourdomain.com"]
allow_credentials=True
```

`.env.example` 增加：
```
CORS_ALLOWED_ORIGINS=["https://yourdomain.com"]
```

---

## 5. 数据模型补全

### 5.1 `analysis_results` 表补全

原 schema `tenant_id` 有 `NOT NULL` 约束，但代码写 `None`。现在 `tenant_id` 从 Flow 参数透传，无需改表。

确认写库时字段映射：

```
Flow 参数 tenant_id (str)
  → AnalysisState.tenant_id
  → store_result(tenant_id=...) task
  → INSERT INTO analysis_results (tenant_id, user_id, ...) VALUES (...)
```

### 5.2 BehaviorProfile 模型（新增）

将行为分析输出从 `dict[str, Any]` 提升为具名 Pydantic 模型，提升类型安全性和可读性：

```
BehaviorProfile
  ├── playstyle: Literal["aggressive","defensive","explorer","collector","social","competitive"]
  ├── current_goal: str
  ├── bottlenecks: list[str]
  ├── resource_status: Literal["normal","abundant","scarce"]
  ├── play_time_pattern: str
  └── engagement_level: Literal["high","medium","low"]
```

`PlayerAnalysisOutput.player_profile` 类型从 `dict[str, Any]` 改为 `BehaviorProfile`。

---

## 6. 开发阶段（修订）

| 阶段 | 内容 | 工作量 | 变更说明 |
|------|------|--------|---------|
| Phase 0 | 基础设施（Docker、uv、`.env`） | 0.5天 | 增加 `rag_storage` volume |
| Phase 1 | 数据库层（SQLAlchemy + Alembic） | 1天 | 修正 `alembic.ini` 占位符 |
| Phase 2 | LightRAG 集成（Neo4j + 文档入库） | 2天 | 不变 |
| Phase 3 | LangGraph Agent（新增 `retrieve_rag_context` 节点，`with_structured_output`） | 3天 | 关键修改 |
| Phase 4 | FastAPI 层（认证中间件落地、限流落地、CORS 修正） | 2天 | 关键修改 |
| Phase 5 | Prefect 调度（Redis 防抖、`tenant_id` 透传、ConcurrencyLimit） | 1.5天 | 关键修改 |
| Phase 6 | 测试 + 部署 | 2天 | 补充认证/限流集成测试 |

**总计**：约 12 个工作日（不变，修改在原有阶段内消化）

---

## 7. 修订后的关键决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| RAG 检索位置 | 主图单次检索，State 注入 | 消除重复 IO，两子图需求高度重叠 |
| 防抖实现 | Redis TTL Key + Prefect cancel | 多实例安全，Redis 是已有基础设施 |
| LLM 输出解析 | `with_structured_output` | 解析失败在 LLM 层报错，比 `json.loads` 错误信息更清晰 |
| RAG 缓存持久化 | 命名 Volume | 容器无状态化，缓存不丢失 |
| 认证缓存 | Redis + PG 双层 | 热路径不打 PG，TTL 可控 |
| 并发控制 | Prefect ConcurrencyLimit | 框架原生支持，跨实例生效 |

---

## 8. 不变的内容

以下设计经评审无问题，保持原方案不变：

- 技术栈选型（LangGraph 1.0、LightRAG v1.4.10、Neo4j 5.x、FastAPI、Prefect 3）
- 目录结构（`src/core/`、`src/api/`、`src/game_specific/`）
- 三表 DB Schema（tenants、quotas、analysis_results）
- LangGraph Checkpointer（PostgresSaver）+ Store（PostgresStore）持久化方案
- Token 配额双层检查（Redis 快速 + PG 持久）
- 熔断器 + tenacity 重试机制（`resilience.py`）
- 测试分层策略（unit / integration / performance）
- Phase 划分顺序

