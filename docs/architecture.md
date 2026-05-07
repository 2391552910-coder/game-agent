# 技术架构

面向开发者的架构详解。包含完整的数据流、模块职责、数据库设计、Agent 图结构和配置体系。

---

## 四层架构

```
┌─────────────────────────────────────────────────────────────┐
│  API 层 (FastAPI)                                           │
│  src/api/                                                   │
│  ├─ 认证中间件 (X-API-Key → Redis → PostgreSQL)             │
│  ├─ 限流中间件 (Redis ZSET 滑动窗口, 100 req/min)           │
│  ├─ Webhook 路由 (接收玩家事件)                              │
│  ├─ Analysis 路由 (查询分析结果)                             │
│  ├─ Tenant 路由 (租户注册)                                   │
│  ├─ Quota 路由 (配额查询)                                    │
│  └─ Provider 路由 (LLM 管理, admin only)                     │
├─────────────────────────────────────────────────────────────┤
│  调度层 (Scheduler)                                          │
│  src/core/scheduler/triggers.py                             │
│  ├─ Redis SET NX 原子去重 (debounce:{user_id}, TTL 300s)    │
│  ├─ asyncio.create_task 后台执行                             │
│  └─ 任务跟踪与取消 (_pending_tasks dict)                     │
├─────────────────────────────────────────────────────────────┤
│  Agent 层 (LangGraph)                                       │
│  src/core/agents/                                           │
│  ├─ 6 节点线性 StateGraph                                    │
│  │   fetch_snapshot → retrieve_rag → gather_context          │
│  │   → behavior_analysis → action_reasoning → merge_output   │
│  ├─ 3 个工具 (历史/相似玩家/RAG 查询)                        │
│  └─ 结构化输出 (Pydantic + with_structured_output)           │
├─────────────────────────────────────────────────────────────┤
│  引擎层 (LightRAG + Storage)                                │
│  src/core/engine/lightrag_engine.py                         │
│  ├─ 向量存储: Milvus (HNSW, COSINE)                         │
│  ├─ 图存储: Neo4j (知识图谱)                                 │
│  ├─ KV 存储: Redis (缓存 + 文档状态)                        │
│  ├─ 业务数据: PostgreSQL (租户/配额/结果)                    │
│  ├─ LLM: 统一工厂 (DeepSeek/OpenAI/Anthropic)               │
│  ├─ Embedding: Qwen text-embedding-v4                       │
│  └─ Rerank: Qwen gte-rerank-v2                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 完整数据流

以"玩家离线触发分析"为例，从 Webhook 到存储结果的完整时序：

```
游戏服务器              API 层                 调度层                Agent 层              存储层
    │                    │                      │                     │                    │
    │ POST /webhooks     │                      │                     │                    │
    │ player-event       │                      │                     │                    │
    │ (offline)          │                      │                     │                    │
    │───────────────────►│                      │                     │                    │
    │                    │                      │                     │                    │
    │                    │ AuthMiddleware        │                     │                    │
    │                    │ X-API-Key → Redis     │                     │                    │
    │                    │   → PostgreSQL        │                     │                    │
    │                    │                      │                     │                    │
    │                    │ RateLimitMiddleware   │                     │                    │
    │                    │ Redis ZSET check      │                     │                    │
    │                    │                      │                     │                    │
    │                    │ schedule_offline_     │                     │                    │
    │                    │ analysis()            │                     │                    │
    │                    │─────────────────────►│                     │                    │
    │                    │                      │                     │                    │
    │                    │                      │ Redis SET NX        │                    │
    │                    │                      │ debounce:{uid}      │                    │
    │                    │                      │────────────────────────────────────────►│
    │                    │                      │                     │    Redis            │
    │                    │                      │                     │                    │
    │                    │  200 {status:         │ asyncio.create_     │                    │
    │◄───────────────────│  "scheduled"}        │ task(bg_analysis)   │                    │
    │                    │                      │────────────────────►│                    │
    │                    │                      │                     │                    │
    │                    │                      │          fetch_player_snapshot()          │
    │                    │                      │          ◄────────── 游戏数据库/模拟       │
    │                    │                      │                     │                    │
    │                    │                      │          Node 1: fetch_snapshot           │
    │                    │                      │          ├─ 验证 snapshot 非空            │
    │                    │                      │                     │                    │
    │                    │                      │          Node 2: retrieve_rag_context     │
    │                    │                      │          ├─ _build_rag_query(snapshot)    │
    │                    │                      │          ├─ LightRAG.aquery(mode=hybrid)  │
    │                    │                      │          │  ├─ Milvus 向量检索             │
    │                    │                      │          │  ├─ Neo4j 图谱检索              │
    │                    │                      │          │  └─ Redis 缓存命中             │
    │                    │                      │                     │                    │
    │                    │                      │          Node 3: gather_context           │
    │                    │                      │          ├─ 快速 LLM + 工具调用            │
    │                    │                      │          ├─ query_player_history (PG)     │
    │                    │                      │          ├─ query_similar_players (PG)     │
    │                    │                      │          └─ dynamic_rag_query (LightRAG)  │
    │                    │                      │          (最多 3 轮, 8 次工具调用)         │
    │                    │                      │                     │                    │
    │                    │                      │          Node 4: behavior_analysis        │
    │                    │                      │          ├─ 快速 LLM                    │
    │                    │                      │          └─ BehaviorProfile 结构化输出     │
    │                    │                      │                     │                    │
    │                    │                      │          Node 5: action_reasoning         │
    │                    │                      │          ├─ 主力 LLM                    │
    │                    │                      │          └─ ActionList 结构化输出         │
    │                    │                      │                     │                    │
    │                    │                      │          Node 6: merge_output             │
    │                    │                      │          └─ PlayerAnalysisOutput 组装     │
    │                    │                      │                     │                    │
    │                    │                      │          store_analysis()                 │
    │                    │                      │          ├─ SHA256 快照去重               │
    │                    │                      │          └─ INSERT INTO analysis_results  │
    │                    │                      │                     │───────────────────►│
    │                    │                      │                     │              PostgreSQL
    │                    │                      │                     │                    │
```

**关键时间线**：
- Webhook 响应立即返回（异步，不等分析完成）
- 后台分析总时长：通常 10-30 秒
  - RAG 检索：~3-5 秒
  - 工具收集：~5-10 秒（快速模型）
  - 行为分析：~3-5 秒（快速模型）
  - 行动推理：~5-10 秒（主力模型）

---

## 目录结构与模块职责

```
src/
├── api/                              # API 层
│   ├── main.py                       # FastAPI 实例、中间件挂载、路由注册、生命周期
│   ├── middleware.py                  # AuthMiddleware (API Key 认证)
│   │                                 # RateLimitMiddleware (滑动窗口限流)
│   └── routes/
│       ├── webhooks.py               # POST /webhooks/player-event
│       ├── analysis.py               # GET /api/v1/analysis/{user_id}/latest|history
│       ├── tenants.py                # POST /api/v1/tenants/register
│       ├── quota.py                  # GET /api/v1/quota/usage
│       └── providers.py              # CRUD /api/v1/providers (admin)
│
├── config.py                         # Pydantic Settings, 从 .env 加载所有配置
│
├── core/
│   ├── agents/                       # LangGraph Agent 层
│   │   ├── orchestrator.py           # build_orchestrator() 构建图
│   │   │                             # create_orchestrator() 带检查点编译
│   │   ├── state.py                  # AnalysisState TypedDict (图的输入输出状态)
│   │   ├── nodes.py                  # 6 个节点函数的实现
│   │   ├── tools.py                  # create_tools() 工厂, 3 个工具
│   │   ├── prompts.py                # 3 套提示词 (上下文收集/行为分析/行动推理)
│   │   └── models.py                 # Pydantic 输出模型
│   │                                 # BehaviorProfile, RecommendedAction,
│   │                                 # PlayerAnalysisOutput, ActionList
│   │
│   ├── engine/
│   │   └── lightrag_engine.py        # LightRAG 全局单例, 存储后端配置,
│   │                                 # LLM/Embedding/Rerank 函数封装
│   │
│   ├── infrastructure/               # 基础设施层
│   │   ├── db.py                     # SQLAlchemy 异步引擎, get_session()
│   │   ├── redis.py                  # Redis 连接池, get_redis()
│   │   ├── neo4j.py                  # Neo4j 异步驱动
│   │   ├── result_store.py           # 分析结果持久化 + SHA256 去重
│   │   └── resilience.py             # CircuitBreaker + with_retry 装饰器
│   │
│   ├── llm/                          # LLM 提供商抽象层
│   │   ├── base.py                   # LLMType = BaseChatModel 类型别名
│   │   ├── models.py                 # LLMProviderConfig/Create/Update/Response
│   │   ├── factory.py                # get_llm(model_type) → BaseChatModel
│   │   │                             # 优先 balancer → 回退 .env 配置
│   │   ├── balancer.py               # LLMBalancer 加权轮询 + 健康降级
│   │   └── providers/__init__.py     # 提供商注册 (openai/anthropic/deepseek/
│   │                                 #   qwen/zhipu/grok)
│   │
│   └── scheduler/
│       └── triggers.py               # schedule_offline_analysis() Redis 去重
│                                     # cancel_offline_analysis() 取消任务
│                                     # _run_analysis_background() 后台执行
│
└── game_specific/                    # 游戏对接点 (游戏方修改此目录)
    └── connector.py                  # PlayerSnapshot TypedDict 定义
                                      # fetch_player_snapshot() 接口
                                      # validate_snapshot() 校验
                                      # build_game_context() 自然语言转换
```

---

## 数据库 Schema

三张核心表 + 一张 LLM 配置表，通过 Alembic 管理（`alembic/versions/`）。

### tenants — 租户表

```sql
CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL UNIQUE,   -- 租户标识
    api_key     VARCHAR(255) NOT NULL UNIQUE,   -- 认证密钥, 格式: gap_{hex}
    is_active   BOOLEAN DEFAULT TRUE,
    is_admin    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
-- 索引: ix_tenants_user_id, ix_tenants_api_key
```

### quotas — 配额表

```sql
CREATE TABLE quotas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    monthly_limit   BIGINT NOT NULL,             -- 月度 Token 上限
    used            BIGINT DEFAULT 0,            -- 已使用 Token
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, period_start)
);
-- 索引: ix_quotas_tenant_id
```

### analysis_results — 分析结果表

```sql
CREATE TABLE analysis_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         VARCHAR(255) NOT NULL,       -- 玩家 ID
    snapshot_hash   VARCHAR(64) NOT NULL,        -- SHA256 快照去重
    output_json     JSON NOT NULL,               -- PlayerAnalysisOutput
    analyzed_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
-- 索引: ix_analysis_results_tenant_user (tenant_id, user_id)
-- 索引: ix_analysis_results_user_id
```

### llm_providers — LLM 提供商表

```sql
CREATE TABLE llm_providers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(100) NOT NULL,
    provider        VARCHAR(50) NOT NULL,        -- 提供商标识
    model           VARCHAR(100) NOT NULL,
    api_key         VARCHAR(500) NOT NULL,
    base_url        VARCHAR(500) NOT NULL,
    weight          INTEGER DEFAULT 1,           -- 轮询权重
    is_active       BOOLEAN DEFAULT TRUE,
    model_type      VARCHAR(20) DEFAULT 'default', -- default | fast
    provider_type   VARCHAR(50) DEFAULT 'openai', -- openai|anthropic|deepseek|...
    max_tokens      INTEGER,                     -- 最大生成 Token
    timeout         INTEGER DEFAULT 60,          -- 超时秒数
    extra_params    JSON DEFAULT '{}',           -- 额外参数
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
-- 索引: ix_llm_providers_model_type, ix_llm_providers_is_active
```

---

## LangGraph Agent 图结构

### State 定义

```python
class AnalysisState(TypedDict):
    user_id: str                                    # 玩家 ID
    tenant_id: str                                  # 租户 ID (隔离)
    snapshot: dict                                  # 玩家快照数据
    rag_context: str                                # RAG 检索结果
    enriched_context: str                           # 工具收集的额外上下文
    behavior_report: str                            # 行为分析 JSON
    reasoned_actions: Annotated[list[dict], operator.add]  # 推理行动 (追加模式)
    final_output: dict                              # 最终组装结果
    errors: Annotated[list[str], operator.add]      # 错误收集 (追加模式)
```

### 节点详解

| 节点 | 模型 | 输入 | 输出 | 说明 |
|------|------|------|------|------|
| **fetch_snapshot** | - | snapshot | (无修改) | 验证快照非空 |
| **retrieve_rag_context** | - | snapshot | rag_context | 从快照文本值构建查询，LightRAG hybrid 检索 |
| **gather_context** | fast | snapshot + rag_context | enriched_context | LLM 自主决定调用工具（最多 3 轮 8 次） |
| **behavior_analysis** | fast | snapshot + rag + enriched | behavior_report | 输出 BehaviorProfile 结构化模型 |
| **action_reasoning** | default | snapshot + rag + enriched + report | reasoned_actions | 输出 RecommendedAction 列表 |
| **merge_output** | - | report + actions | final_output | 组装 PlayerAnalysisOutput |

### 工具集

工具通过 `create_tools(tenant_id, user_id)` 工厂创建，闭包注入租户和用户上下文：

| 工具 | 数据源 | 用途 |
|------|--------|------|
| `query_player_history` | PostgreSQL | 查询历史分析记录，检测行为趋势 |
| `query_similar_players` | PostgreSQL | 查询同租户下相似玩家，用于对比参考 |
| `dynamic_rag_query` | LightRAG | 动态查询知识库，获取初始检索未覆盖的规则 |

### 防护机制

- **防循环**: `gather_context` 最多 3 轮迭代，累计 8 次工具调用
- **超时**: 每个外部调用 60 秒超时 (`_SINGLE_CALL_TIMEOUT`)
- **错误累积**: `errors` 字段使用 `operator.add` reducer，不会覆盖前序错误
- **结构化输出**: `with_structured_output(method="function_calling")` 确保 LLM 输出合法 JSON

---

## LLM 多提供商负载均衡

### 架构

```
get_llm(model_type="default")
    │
    ├─ 1. 尝试 LoadBalancer (DB 配置的提供商)
    │      ├─ 按 model_type 过滤
    │      ├─ 过滤 is_active=True
    │      ├─ 过滤健康状态 (Redis health key)
    │      └─ 加权轮询选择
    │
    └─ 2. 回退 .env 配置 (OpenAI 兼容端点)
           └─ ChatOpenAI(api_key, base_url, model)
```

### 健康降级

- 每次调用失败，Redis 计数器 `llm:health:{provider_id}` +1
- 连续 5 次失败 → 标记为不健康
- 不健康的 provider 会被跳过
- Redis TTL 1 小时后自动恢复

### 支持的提供商类型

| provider_type | SDK | 说明 |
|---------------|-----|------|
| openai | langchain-openai | OpenAI 官方 API |
| deepseek | langchain-openai | DeepSeek (OpenAI 兼容) |
| anthropic | langchain-anthropic | Anthropic Claude |
| qwen | langchain-openai | 通义千问 (DashScope) |
| zhipu | langchain-openai | 智谱 GLM (OpenAI 兼容) |
| grok | langchain-openai | xAI Grok (OpenAI 兼容) |

---

## LightRAG 存储架构

LightRAG 使用三种存储后端协同工作：

```
查询请求 → LightRAG
              │
              ├─ Milvus (向量存储)
              │   ├─ 索引类型: HNSW
              │   ├─ 相似度: COSINE
              │   └─ 参数: M=24, ef_construction=360, ef=200
              │
              ├─ Neo4j (图存储)
              │   └─ 知识图谱: 实体-关系网络
              │
              └─ Redis (KV + 文档状态)
                  ├─ KV Storage: 缓存
                  └─ DocStatusStorage: 文档处理状态
```

**检索模式**: `hybrid` (默认) — 同时使用向量检索 + 图谱遍历 + 关键词匹配

**文档处理参数**:
- 分块大小: 512 tokens
- 分块重叠: 256 tokens
- 摘要最大长度: 10000 tokens

---

## 配置体系

所有配置通过 `.env` 文件管理，`src/config.py` 使用 `pydantic-settings` 加载。

标记为 `必填` 的字段缺失时应用无法启动。完整字段说明见 [部署指南](deployment.md)。

| 分组 | 关键字段 | 必填 | 默认值 |
|------|---------|------|--------|
| App | ENV, LOG_LEVEL, APP_WORKERS | 否 | development, INFO, 1 |
| LLM | OPENAI_API_KEY, OPENAI_BASE_URL | 是 | - |
| LLM | OPENAI_DEFAULT_MODEL, OPENAI_FAST_MODEL | 否 | deepseek-chat |
| Embedding | EMBEDDING_API_KEY | 是 | - |
| Embedding | EMBEDDING_BASE_URL, EMBEDDING_MODEL | 否 | DashScope, text-embedding-v4 |
| Rerank | RERANK_API_KEY | 是 | - |
| PostgreSQL | POSTGRES_DSN | 是 | - |
| Neo4j | NEO4J_PASSWORD | 是 | - |
| Redis | REDIS_URL | 否 | redis://localhost:6379/0 |
| Milvus | MILVUS_URI | 否 | http://localhost:19530 |
| RAG | RAG_DEFAULT_STRATEGY, RAG_WORKING_DIR | 否 | hybrid, ./rag_storage |
| 调度 | MAX_CONCURRENT_ANALYSES, OFFLINE_TRIGGER_MINUTES | 否 | 20, 5 |
| 配额 | DEFAULT_MONTHLY_TOKENS, QUOTA_WARNING_THRESHOLD | 否 | 40000000, 0.8 |

---

## 设计决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| Agent 框架 | LangGraph | 行业标准，支持状态图 + 检查点 + 子图 |
| RAG 框架 | LightRAG | hybrid 模式比纯向量检索精度提升 30%+ |
| 图数据库 | Neo4j 5 | LightRAG 原生支持 |
| 向量库 | Milvus | 支持亿级向量，HNSW 高性能 |
| 包管理 | uv | 比 pip 快 10-100 倍 |
| 异步框架 | FastAPI | 原生 async，自动 OpenAPI 文档 |
| 调度方式 | asyncio.create_task | 替代 Prefect，减少外部依赖，低延迟 |
| 结构化输出 | with_structured_output | 避免 LLM 返回非法 JSON |
| 去重机制 | Redis SET NX | 原子操作，避免 TOCTOU 竞态 |
| ORM | SQLAlchemy 2.0 async | 异步支持好，团队熟悉 |
