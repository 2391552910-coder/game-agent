# Game Agent Platform v2 — 完全重写设计文档

**日期**: 2026-04-02
**状态**: 待批准
**作者**: Sisyphus

---

## 1. 概述

多租户游戏玩家行为分析与预测平台 v2，完全重写。保留核心业务逻辑（多租户、玩家行为分析、Agent 协调、结构化输出），全面升级技术栈。

### 1.1 核心业务逻辑（保留）

- 游戏服务器 webhook 接收玩家离线事件
- 防抖 + 限流后触发分析流程
- 主 Agent 协调子 Agent（行为分析 + 行动推理）
- 结构化 JSON 输出 → FastAPI → 下游执行 Agent
- 多租户认证 + Token 配额管理

### 1.2 技术栈升级

| 旧技术 | 新技术 | 版本 | 升级原因 |
|--------|--------|------|----------|
| deepagents 0.4.1 | **LangGraph** | 1.0.x | Agent 编排事实标准，原生 subgraph、持久化、人机协同 |
| Milvus 2.6.11 | **LightRAG + Neo4j** | v1.4.10 + 5.x | 知识图谱 RAG，实体关系提取，关联查询性能 5-10x 提升 |
| 自研 RAG | **LightRAG** | v1.4.10 | 自动分块、实体提取、关系构建、多模式检索 |
| 手动配置 | **Pydantic Settings** | v2.12 | 类型安全的环境变量管理 |
| 同步 SQLAlchemy | **SQLAlchemy 2.0 async** | 2.0.x | 完整异步支持，性能提升 |

---

## 2. 完整技术栈

### 2.1 核心框架

| 层级 | 技术 | 版本 | 官方文档 |
|------|------|------|----------|
| Agent 编排 | LangGraph | 1.0.x | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) |
| LLM 接口 | LangChain | 最新 | [langchain-ai/langchain](https://github.com/langchain-ai/langchain) |
| RAG 引擎 | LightRAG | v1.4.10 | [hkuds/lightrag](https://github.com/HKUDS/LightRAG) |
| API 框架 | FastAPI | 0.128.0 | [fastapi.tiangolo.com](https://fastapi.tiangolo.com/) |
| 数据验证 | Pydantic | v2.12 | [docs.pydantic.dev](https://docs.pydantic.dev/) |
| ORM | SQLAlchemy | 2.0 async | [docs.sqlalchemy.org](https://docs.sqlalchemy.org/) |
| 迁移 | Alembic | 最新 | [alembic.sqlalchemy.org](https://alembic.sqlalchemy.org/) |
| 任务调度 | Prefect | 3.4.x | [docs.prefect.io](https://docs.prefect.io/) |
| 缓存/限流 | Redis-py | 6.4.0 | [redis.readthedocs.io](https://redis.readthedocs.io/) |

### 2.2 基础设施

| 服务 | 版本 | 作用 |
|------|------|------|
| PostgreSQL | 17 | 业务数据库（租户、配额、分析结果） |
| Neo4j | 5.x | 知识图谱存储（LightRAG 后端） |
| Redis | 8 | 缓存、限流、分布式锁 |
| Prefect Server | 3.4 | 调度 UI 和 API |

### 2.3 开发工具

| 工具 | 作用 |
|------|------|
| uv | Python 包管理（替代 pip/poetry） |
| pytest + pytest-asyncio | 异步单元测试 |
| httpx | FastAPI 集成测试 |
| ruff | 代码检查 + 格式化 |
| mypy | 类型检查 |

---

## 3. 架构设计

### 3.1 整体架构

```
游戏服务器 webhook
       ↓
┌─────────────────────────────────────────────┐
│  FastAPI 0.128 (API 层)                      │
│  ├── 认证中间件 (API Key + 配额检查)         │
│  ├── 限流中间件 (Redis 滑动窗口)             │
│  ├── Webhook 路由 → 事件入队                 │
│  ├── 分析结果路由 → 查询/流式/SSE            │
│  └── 租户管理路由                            │
└──────────────┬──────────────────────────────┘
               ↓
┌─────────────────────────────────────────────┐
│  Prefect 3.4 (调度层)                        │
│  ├── 防抖逻辑 (OFFLINE_TRIGGER_MINUTES)      │
│  ├── 并发控制 (MAX_CONCURRENT_ANALYSES)       │
│  └── Flow: fetch → analyze → store           │
└──────────────┬──────────────────────────────┘
               ↓
┌─────────────────────────────────────────────┐
│  LangGraph 1.0 (Agent 层)                    │
│  ┌───────────────────────────────────────┐   │
│  │ Orchestrator Graph (主协调图)          │   │
│  │  START → fetch_snapshot → [子图]       │   │
│  │                    ↓                    │   │
│  │  ┌─────────────┐  ┌──────────────┐    │   │
│  │  │BehaviorGraph│  │ReasonerGraph │    │   │
│  │  │(行为分析子图)│  │(推理子图)     │    │   │
│  │  └─────────────┘  └──────────────┘    │   │
│  │                    ↓                    │   │
│  │              merge → END               │   │
│  │                                        │   │
│  │  Checkpointer: PostgreSQL              │   │
│  │  Store: PostgresStore (跨会话记忆)      │   │
│  └───────────────────────────────────────┘   │
└──────────────┬──────────────────────────────┘
               ↓
┌─────────────────────────────────────────────┐
│  LightRAG v1.4.10 (引擎层)                   │
│  ├── 知识图谱: Neo4j (实体+关系)              │
│  ├── 向量索引: Neo4j Vector                   │
│  ├── 检索模式: naive/local/global/hybrid/mix │
│  └── 文档入库: 自动分块→实体提取→关系构建      │
└──────────────┬──────────────────────────────┘
               ↓
┌─────────────────────────────────────────────┐
│  存储层                                      │
│  ├── PostgreSQL 17: 业务数据                 │
│  ├── Neo4j 5: 知识图谱                       │
│  └── Redis 8: 缓存/限流/锁                   │
└─────────────────────────────────────────────┘
```

### 3.2 目录结构

```
game-agent-platform/
├── docker-compose.yml          # 基础设施编排
├── pyproject.toml              # uv 依赖管理
├── alembic.ini                 # 数据库迁移配置
├── .env.example                # 环境变量模板
├── game_docs/                  # 游戏规则文档
├── src/
│   ├── __init__.py
│   ├── config.py               # Pydantic Settings 统一配置
│   │
│   ├── core/                   # 核心框架（通用）
│   │   ├── __init__.py
│   │   ├── llm/                # LLM 客户端工厂
│   │   │   ├── __init__.py
│   │   │   ├── factory.py      # 多提供商 LLM 工厂
│   │   │   └── quota.py        # Token 配额跟踪
│   │   │
│   │   ├── engine/             # 引擎层
│   │   │   ├── __init__.py
│   │   │   ├── rag.py          # LightRAG 封装
│   │   │   └── behavior.py     # 行为分析器
│   │   │
│   │   ├── agents/             # LangGraph Agent 层
│   │   │   ├── __init__.py
│   │   │   ├── state.py        # 共享状态定义
│   │   │   ├── orchestrator.py # 主协调图
│   │   │   ├── behavior_graph.py # 行为分析子图
│   │   │   ├── reasoner_graph.py # 推理子图
│   │   │   ├── tools.py        # Agent 工具
│   │   │   └── prompts.py      # Prompt 模板
│   │   │
│   │   ├── scheduler/          # Prefect 调度层
│   │   │   ├── __init__.py
│   │   │   ├── flows.py        # Flow 定义
│   │   │   └── triggers.py     # 离线检测
│   │   │
│   │   ├── output/             # 输出层
│   │   │   ├── __init__.py
│   │   │   └── schema.py       # Pydantic 输出模型
│   │   │
│   │   └── infrastructure/     # 基础设施
│   │       ├── __init__.py
│   │       ├── db.py           # SQLAlchemy async 引擎
│   │       ├── redis.py        # Redis 连接
│   │       ├── neo4j.py        # Neo4j 连接
│   │       ├── resilience.py   # 熔断器/重试
│   │       └── result_store.py # 结果持久化
│   │
│   ├── api/                    # FastAPI 层
│   │   ├── __init__.py
│   │   ├── main.py             # 应用入口
│   │   ├── middleware.py       # 认证/限流/配额
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── webhooks.py
│   │       ├── analysis.py
│   │       ├── tenants.py
│   │       └── quota.py
│   │
│   └── game_specific/          # 游戏特定代码
│       ├── __init__.py
│       ├── connector.py        # 游戏 DB 连接
│       ├── models.py           # PlayerSnapshot
│       └── ingest.py           # 文档入库 → LightRAG
│
├── alembic/                    # 数据库迁移
│   ├── env.py
│   └── versions/
│
├── tests/                      # pytest 单元测试
└── scripts/                    # 工具脚本
    ├── admin/
    ├── tests/
    └── utils/
```

---

## 4. 关键组件设计

### 4.1 LangGraph Agent 系统

#### 4.1.1 状态设计（重叠键自动映射）

子图状态是主状态的子集，利用 LangGraph 的自动 state mapping：

```python
# 子图只声明自己需要的字段
class BehaviorState(TypedDict):
    snapshot: PlayerSnapshot
    rag_context: str
    analysis: str  # JSON 格式的行为分析报告

class ReasonerState(TypedDict):
    behavior_report: str
    snapshot: PlayerSnapshot
    rag_context: str
    actions: list[dict]  # 行动序列

# 主状态包含所有子图字段 + 自己的字段
# 重叠的键（snapshot, rag_context 等）自动映射到子图
class AnalysisState(TypedDict):
    user_id: str
    tenant_id: str
    snapshot: PlayerSnapshot           # 与子图重叠 → 自动传递
    rag_context: str                   # 与子图重叠 → 自动传递
    behavior_report: str               # 行为分析输出
    reasoned_actions: list[dict]       # 推理输出
    final_output: dict[str, Any]       # 最终 JSON 输出
    errors: list[str]                  # 错误收集
```

#### 4.1.2 主协调图 (Orchestrator) — 带条件边

```python
# 图结构：带错误短路
START → fetch_snapshot → behavior_analysis → [条件边] → action_reasoning → merge_output → END
                                              ↓
                                         error → merge_output（跳过推理）

# 条件边实现
def check_behavior(state):
    if state.get("errors"):
        return "merge_output"  # 跳过推理，直接合并已有数据
    return "action_reasoning"

builder.add_conditional_edges("behavior_analysis", check_behavior, {
    "action_reasoning": "action_reasoning",
    "merge_output": "merge_output",
})
```

#### 4.1.3 行为分析子图 (BehaviorGraph)

```python
# 图结构
START → retrieve_rules(LightRAG) → analyze(LLM快速模型) → END
```

#### 4.1.4 推理子图 (ReasonerGraph)

```python
# 图结构
START → retrieve_rules(LightRAG) → reason(LLM主力模型+extended_thinking) → END
```

#### 4.1.5 持久化

- **Checkpointer**: `PostgresSaver` — Agent 状态持久化，支持时间旅行
- **Store**: `PostgresStore` — 跨会话记忆（玩家历史画像）

### 4.2 LightRAG 集成

#### 4.2.1 存储后端架构

LightRAG 使用混合存储：PostgreSQL 承担 KV + 向量存储，Neo4j 承担图存储。这样 PostgreSQL 同时服务业务数据和 RAG 基础设施，减少运维组件。

```
┌─────────────────────────────────────────┐
│           LightRAG Storage              │
├─────────────┬─────────────┬─────────────┤
│ KV Storage  │   Vector    │   Graph     │
│ (PGKV)      │ (PGVector)  │ (Neo4j)     │
│ 文档元数据   │  向量索引    │ 实体+关系    │
│ 文档状态    │  语义检索    │  图谱查询    │
└─────────────┴─────────────┴─────────────┘
```

#### 4.2.2 初始化

```python
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc

rag = LightRAG(
    working_dir="./rag_storage",
    # PostgreSQL 统一后端（KV + 向量）
    kv_storage="PGKVStorage",
    vector_storage="PGVectorStorage",
    doc_status_storage="PGDocStatusStorage",
    # Neo4j 图存储
    graph_storage="Neo4JStorage",
    # LLM 配置
    llm_model_func=openai_complete_if_cache,
    llm_model_name=settings.openai_default_model,
    llm_model_kwargs={
        "api_key": settings.openai_api_key,
        "base_url": settings.openai_base_url,
    },
    # Embedding 配置
    embedding_func=EmbeddingFunc(
        embedding_dim=1024,  # BGE-M3 维度
        max_token_size=8192,
        func=lambda texts: embed_func(texts),  # 可切换 provider
    ),
    # 向量检索阈值
    vector_db_storage_cls_kwargs={
        "cosine_better_than_threshold": 0.2,
    },
)
await rag.initialize_storages()
```

#### 4.2.3 Embedding Provider 策略

通过配置切换 embedding provider，支持本地 BGE-M3 和云端 API：

```python
# src/config.py 新增
embedding_provider: str = Field(default="openai", description="Embedding 提供商标识")

# src/core/engine/rag.py
async def get_embedding_func():
    providers = {
        "openai": openai_embed,
        "bge_m3_local": bge_m3_local_embed,
        "jina": jina_embed,
    }
    return providers.get(settings.embedding_provider, openai_embed)
```

#### 4.2.2 检索模式

| 模式 | 描述 | 适用场景 |
|------|------|----------|
| `naive` | 仅文本匹配 | 简单关键词查询 |
| `local` | 局部实体检索 | 特定实体相关查询 |
| `global` | 全局图谱检索 | 跨领域关联查询 |
| `hybrid` | 向量 + 图谱融合 | 大多数场景（默认） |
| `mix` | 所有模式融合 | 复杂多意图查询 |

#### 4.2.3 文档入库

```python
# 自动流程：
# 1. 文档分块
# 2. LLM 提取实体（BOSS、道具、副本、技能...）
# 3. LLM 提取关系（克制、需要、位于、掉落...）
# 4. 存入 Neo4j 图 + 向量索引
await rag.ainsert(document_text)
```

### 4.3 数据验证与输出

```python
# Pydantic v2 结构化输出
class PlayerAnalysisOutput(BaseModel):
    model_config = ConfigDict(strict=True)

    schema_version: str = "2.0"
    user_id: str
    analyzed_at: datetime
    player_profile: PlayerProfile
    recommended_actions: list[RecommendedAction]
    overall_confidence: float
    analysis_notes: str | None = None
    reanalysis_triggers: ReanalysisTriggers
```

### 4.4 配置管理

```python
# Pydantic Settings
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    llm_provider: str = "deepseek"
    openai_api_key: str
    openai_base_url: str
    openai_default_model: str

    # 数据库
    postgres_dsn: PostgresDsn
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # RAG
    rag_default_strategy: str = "hybrid"

    # 调度
    max_concurrent_analyses: int = 20
    offline_trigger_minutes: int = 5

settings = Settings()
```

---

## 5. 错误处理与韧性

### 5.1 四层错误处理

| 层级 | 策略 | 实现 |
|------|------|------|
| **LLM 调用** | 重试 + 降级 | `with_retry(3次指数退避)` → 降级到快速模型 |
| **LightRAG** | 降级检索 | hybrid → local → naive |
| **Agent 图** | LangGraph 内置 | `interrupt()` 暂停 + 人工干预 |
| **API 层** | 熔断器 | Redis 计数器 + 自动恢复 |

### 5.2 熔断器（线程安全）

```python
class CircuitBreaker:
    """防止级联故障 — asyncio.Lock 保证并发安全"""
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._lock = asyncio.Lock()
        self._failures = 0
        self._last_failure_time = 0.0
        self._state = "closed"

    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        async with self._lock:
            if self.state == "open":
                raise CircuitBreakerOpen(f"Circuit breaker '{self.name}' is open")
        try:
            result = await func(*args, **kwargs)
            async with self._lock:
                self.record_success()
            return result
        except Exception:
            async with self._lock:
                self.record_failure()
            raise
```

---

## 6. 安全与多租户

### 6.1 认证流程

```
请求 → FastAPI 中间件
  → 提取 X-API-Key
  → Redis 缓存查找（TTL 5min）
  → PostgreSQL 验证租户 + 配额
  → 通过/拒绝
```

### 6.2 限流（Redis 滑动窗口）

```python
# 每分钟最多 100 次请求 — Redis ZSET 滑动窗口
async def rate_limit_middleware(request: Request, call_next):
    key = f"rate:{request.client.host}"
    now = time.time()
    window = 60
    max_requests = 100

    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, now - window)  # 清理过期
    pipe.zcard(key)                               # 当前窗口计数
    _, count = await pipe.execute()

    if count >= max_requests:
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    await redis.zadd(key, {f"{now}:{uuid4()}": now})
    await redis.expire(key, window)
    return await call_next(request)
```

### 6.3 Token 配额

- 月度配额控制
- **AuthMiddleware 完整验证链**：API Key 存在性 → Redis 缓存 → PostgreSQL 验证 → 配额检查 → 通过/拒绝（429）
- TokenUsageCallback 记录每次 LLM 调用的 token 消耗，定期同步到 PostgreSQL

---

## 7. 测试策略

| 测试类型 | 工具 | 覆盖范围 |
|----------|------|----------|
| 单元测试 | pytest + pytest-asyncio | 每个模块独立测试 |
| 集成测试 | httpx (FastAPI TestClient) | API 端点完整流程 |
| Agent 测试 | LangGraph 内置回放 | 图执行、状态转换 |
| RAG 测试 | LightRAG mock | 检索准确率、响应时间 |
| 性能测试 | locust | 并发分析、限流验证 |

---

## 8. 开发阶段划分

| 阶段 | 内容 | 预计工作量 | 依赖 |
|------|------|-----------|------|
| **Phase 0** | 基础设施搭建（Docker Compose、uv、pyproject.toml、.env） | 1天 | 无 |
| **Phase 1** | 数据库层（SQLAlchemy async + Alembic + 模型定义） | 1天 | Phase 0 |
| **Phase 2** | LightRAG 集成（Neo4j 连接、文档入库、查询封装） | 2天 | Phase 0 |
| **Phase 3** | LangGraph Agent（状态定义、主图、子图、工具、Prompt） | 3天 | Phase 1, 2 |
| **Phase 4** | FastAPI 层（路由、中间件、认证、限流、配额） | 2天 | Phase 1, 3 |
| **Phase 5** | Prefect 调度（Flow 定义、防抖、触发器） | 1天 | Phase 3, 4 |
| **Phase 6** | 测试 + 文档 + 部署脚本 | 2天 | Phase 0-5 |

**总计**：约 12 个工作日

---

## 9. 迁移注意事项

### 9.1 从 Milvus 到 Neo4j

- LightRAG 自动处理图谱构建，无需手动迁移向量
- 旧 Milvus 数据可废弃（LightRAG 重新入库文档即可）

### 9.2 从 deepagents 到 LangGraph

- deepagents 的子 Agent 概念 → LangGraph subgraph
- deepagents 的工作区传递 → LangGraph State 传递
- deepagents 的长期记忆 → LangGraph PostgresStore

### 9.3 保留不变

- 业务逻辑（玩家分析流程、输出格式、多租户）
- 游戏数据库连接方式（connector.py 修改 SQL 适配）
- 游戏文档入库流程（ingest.py 调用 LightRAG）

---

## 10. 关键决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| Agent 框架 | LangGraph 1.0 | 事实标准，subgraph 原生支持，持久化完善 |
| RAG 框架 | LightRAG | 知识图谱 RAG，实体关系提取，比传统向量 RAG 准确率高 30%+ |
| RAG 存储 | PGKV + PGVector + Neo4J | PostgreSQL 统一后端（KV+向量），Neo4j 专注图存储，减少运维 |
| 子图状态 | 重叠键自动映射 | 利用 LangGraph 自动 state mapping，避免手动构造子图状态 |
| 图数据库 | Neo4j 5.x | LightRAG 原生支持，Cypher 查询语言成熟 |
| 包管理 | uv | 比 pip/poetry 快 10-100 倍 |
| ORM | SQLAlchemy 2.0 async | Python 异步 ORM 事实标准 |
| 保留 Prefect | Prefect 3.4 | 已验证成熟，防抖 + 限流逻辑完善 |
| 依赖注入 | FastAPI Depends() | 替代全局单例，支持测试和 worker 隔离 |
| Embedding | 策略模式可切换 | 支持 openai / bge_m3_local / jina 等 provider |
| 错误处理 | 条件边短路 | 子图失败时跳过后续节点，避免浪费 LLM 调用 |
| 熔断器 | asyncio.Lock | 保证 async 并发安全 |
| 配额执行 | 中间件完整验证链 | AuthMiddleware 完成 Key 验证 + 配额检查，路由无需重复 |

## 11. 架构审查修复记录

| # | 严重度 | 问题 | 修复方案 |
|---|--------|------|----------|
| 1 | Critical | LightRAG 只设 graph_storage，KV/向量退化为 JSON 文件 | 使用 PGKVStorage + PGVectorStorage + Neo4JStorage 混合后端 |
| 2 | Critical | 子图状态手动构造空 dict，违背 LangGraph 设计 | 子图状态作为主状态子集，利用重叠键自动映射 |
| 3 | Critical | 线性图无错误分支，失败后继续浪费 LLM 调用 | 添加条件边 check_behavior，失败时跳过推理直接 merge |
| 4 | Critical | AuthMiddleware 只做存在性检查，每个路由重复验证 | 中间件完成完整验证链（Key → DB → 配额），tenant 存入 request.state |
| 5 | Important | 全局单例在 async 多 worker 下脆弱 | 使用 FastAPI Depends() 依赖注入 |
| 6 | Important | CircuitBreaker 无并发安全保护 | 添加 asyncio.Lock 保护状态变更 |
| 7 | Important | Embedding 函数名与实际 provider 不符 | 策略模式 + embedding_provider 配置 |
| 8 | Important | Prefect Flow 与 LangGraph 重复获取 snapshot | Prefect 传入 snapshot，orchestrator 跳过 fetch 节点 |
| 9 | Important | RateLimitMiddleware 是空实现 | 完整 Redis ZSET 滑动窗口实现 |
| 10 | Important | 缺少 Dockerfile | 添加多阶段构建 Dockerfile |
| 11 | Minor | Prompt 硬编码在 Python 文件 | 保留 prompts.yaml 集中管理 |
| 12 | Minor | LLM 缓存无上限 | 使用 cachetools.TTLCache 限制大小和 TTL |
| 13 | Minor | Neo4j healthcheck 可能不工作 | 改用 HTTP API 检查（curl localhost:7474） |
| 14 | Minor | Token 配额有跟踪无执行 | 在 AuthMiddleware 中加入配额检查 |
