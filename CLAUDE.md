# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

myAgent v2.0 是一个多租户游戏玩家行为分析与预测平台。游戏服务器通过 Webhook 发送玩家数据，平台使用 LangGraph Agent 编排 + LightRAG 知识图谱检索，生成结构化的玩家行为分析和推荐行动。

## Common Commands

```bash
# 启动基础设施（PostgreSQL, Redis, Neo4j, Milvus+etcd+MinIO, Prefect）
docker-compose -f docker-compose.dev.yml up -d

# 安装依赖（使用 uv 包管理器）
uv sync

# 数据库迁移
alembic upgrade head

# 运行测试
pytest tests/ -v

# LightRAG 集成测试（需要基础设施全部启动 + .env 配置）
python -m scripts.tests.test_lightrag

# 代码检查
ruff check src/
mypy src/

# 格式化
ruff format src/
```

## Architecture

### 四层架构

```
FastAPI (API 层) → Prefect (调度层) → LangGraph (Agent 层) → LightRAG (引擎层)
```

**数据流**: 游戏服务器 Webhook → Redis 去重 → Prefect 延迟调度 → LangGraph Agent 协调分析 → LightRAG 提供游戏知识上下文 → 结构化 JSON 返回游戏

### LangGraph Agent 结构

- `AnalysisState` (state.py): 有状态图的状态定义，包含 user_id, tenant_id, snapshot, rag_context, behavior_report, reasoned_actions, final_output, errors
- `BehaviorProfile` / `RecommendedAction` / `PlayerAnalysisOutput` (models.py): Pydantic 结构化输出模型
- 节点 (nodes.py): behavior_analysis, action_reasoning, merge_output（待实现）

### LightRAG 引擎 (lightrag_engine.py)

全局单例模式，通过 `get_rag(workspace=)` 获取。多租户隔离: Milvus 通过 collection 前缀，Neo4j 通过 Label。

存储后端映射:
- KV 缓存/文档状态 → Redis
- 知识图谱 → Neo4j
- 向量检索 → Milvus
- 业务数据（租户/配额/分析结果）→ PostgreSQL

LLM 使用 DeepSeek（OpenAI 兼容端点），Embedding 使用 Qwen text-embedding-v4，Rerank 使用 gte-rerank-v2。

### 数据库 (infrastructure/db.py)

SQLAlchemy 2.0 异步引擎，使用 `get_session()` 上下文管理器自动处理 commit/rollback。开发环境 echo=True 打印 SQL。

### 配置 (config.py)

所有配置通过 `pydantic-settings` 从 `.env` 文件加载，访问 `from src.config import settings`。必填字段缺少时会阻止启动。

## Key Conventions

- Python 3.11-3.12，异步优先（async/await）
- 包管理使用 `uv`，构建后端为 `hatchling`
- Ruff: target py311, line-length 120, lint rules: E/F/I/N/W/UP/B/SIM
- mypy strict mode
- pytest-asyncio auto mode
- Commit message 使用中文（如 `feat: 添加核心基础设施`）
- 代码注释和文档使用中文

## Database Schema

三张核心表（alembic/versions/001_initial.py）:
- `tenants`: 多租户，api_key 认证，user_id 唯一
- `quotas`: 按月周期记录 token 使用量，tenant_id + period_start 联合唯一
- `analysis_results`: 分析结果，snapshot_hash 去重，tenant_id 非空确保隔离

## Module Status

已实现: config, db infrastructure, LightRAG engine, agent state/models, database migration, docker-compose dev environment
待实现: API routes/middleware, LangGraph agent nodes/edges, Prefect scheduling flows, multi-tenant auth, rate limiting, game connectors

## Design Docs

详细设计和实现路线图在 `specs/` 目录:
- `game-agent-platform-v2-design-optimized.md` - 架构总览（含修正数据流）
- `2026-04-02-game-agent-platform-v2-implementation.md` - 实现路线图
