# Game Agent Platform v2 实现计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从零开始构建基于 LangGraph + LightRAG + Neo4j + FastAPI 的多租户游戏玩家行为分析平台。

**Architecture:** LangGraph 1.0 编排多 Agent（主图+子图），LightRAG v1.4.10 构建知识图谱 RAG（Neo4j 后端），FastAPI 0.128 提供 REST API，SQLAlchemy 2.0 async 操作 PostgreSQL，Prefect 3.4 调度离线分析任务。

**Tech Stack:** LangGraph 1.0, LightRAG v1.4.10, Neo4j 5.x, FastAPI 0.128, Pydantic v2.12, SQLAlchemy 2.0 async, Alembic, Prefect 3.4, Redis 8, PostgreSQL 17, uv

---

## 文件映射总览

### 新建文件

| 文件 | 职责 |
|------|------|
| `pyproject.toml` | uv 依赖管理，替代 requirements.txt |
| `docker-compose.yml` | 全基础设施编排（PG + Neo4j + Redis + Prefect） |
| `.env.example` | 环境变量模板 |
| `alembic.ini` | Alembic 配置 |
| `alembic/env.py` | Alembic 异步迁移环境 |
| `alembic/versions/001_initial.py` | 初始数据库迁移 |
| `src/__init__.py` | 包初始化 |
| `src/config.py` | Pydantic Settings 统一配置 |
| `src/core/__init__.py` | 核心框架包 |
| `src/core/infrastructure/__init__.py` | 基础设施包 |
| `src/core/infrastructure/db.py` | SQLAlchemy async 引擎 + 会话管理 |
| `src/core/infrastructure/redis.py` | Redis 异步连接池 |
| `src/core/infrastructure/neo4j.py` | Neo4j 异步驱动 |
| `src/core/infrastructure/resilience.py` | 熔断器 + 重试装饰器 |
| `src/core/infrastructure/result_store.py` | 分析结果持久化到 PostgreSQL |
| `src/core/llm/__init__.py` | LLM 客户端包 |
| `src/core/llm/factory.py` | 多提供商 LLM 工厂 |
| `src/core/llm/quota.py` | Token 配额跟踪 |
| `src/core/engine/__init__.py` | 引擎层包 |
| `src/core/engine/rag.py` | LightRAG 封装 |
| `src/core/engine/behavior.py` | 行为分析器 |
| `src/core/agents/__init__.py` | Agent 层包 |
| `src/core/agents/state.py` | LangGraph 状态定义 |
| `src/core/agents/orchestrator.py` | 主协调图 |
| `src/core/agents/behavior_graph.py` | 行为分析子图 |
| `src/core/agents/reasoner_graph.py` | 推理子图 |
| `src/core/agents/tools.py` | Agent 工具函数 |
| `src/core/agents/prompts.py` | Prompt 模板 |
| `src/core/output/__init__.py` | 输出层包 |
| `src/core/output/schema.py` | Pydantic 输出模型 |
| `src/core/scheduler/__init__.py` | 调度层包 |
| `src/core/scheduler/flows.py` | Prefect Flow 定义 |
| `src/core/scheduler/triggers.py` | 离线检测触发器 |
| `src/api/__init__.py` | API 层包 |
| `src/api/main.py` | FastAPI 应用入口 |
| `src/api/middleware.py` | 认证 + 限流 + 配额中间件 |
| `src/api/routes/__init__.py` | 路由包 |
| `src/api/routes/webhooks.py` | Webhook 端点 |
| `src/api/routes/analysis.py` | 分析结果端点 |
| `src/api/routes/tenants.py` | 租户管理端点 |
| `src/api/routes/quota.py` | 配额管理端点 |
| `src/game_specific/__init__.py` | 游戏特定代码包 |
| `src/game_specific/connector.py` | 游戏数据库连接 |
| `src/game_specific/models.py` | PlayerSnapshot 数据模型 |
| `src/game_specific/ingest.py` | 文档入库 → LightRAG |
| `scripts/admin/switch_llm.py` | LLM 提供商切换 |
| `scripts/admin/manage_api_keys.py` | API Key 管理 |
| `scripts/admin/manage_quota.py` | Token 配额管理 |
| `scripts/tests/test_full.py` | 快速集成测试 |
| `scripts/utils/check_env.py` | 环境配置检查 |
| `tests/test_config.py` | 配置测试 |
| `tests/test_db.py` | 数据库层测试 |
| `tests/test_rag.py` | RAG 引擎测试 |
| `tests/test_agents.py` | Agent 图测试 |
| `tests/test_api.py` | API 集成测试 |
| `tests/test_output_schema.py` | 输出模型测试 |

### 修改文件

无（完全重写，旧代码保留在 git 历史中）

---

## Chunk 1: 基础设施与数据库层

### Task 1: 项目初始化 — pyproject.toml + docker-compose.yml + .env.example

**Files:**
- Create: `pyproject.toml`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `src/__init__.py`
- Create: `src/core/__init__.py`
- Create: `src/core/infrastructure/__init__.py`
- Create: `src/core/llm/__init__.py`
- Create: `src/core/engine/__init__.py`
- Create: `src/core/agents/__init__.py`
- Create: `src/core/output/__init__.py`
- Create: `src/core/scheduler/__init__.py`
- Create: `src/api/__init__.py`
- Create: `src/api/routes/__init__.py`
- Create: `src/game_specific/__init__.py`

- [ ] **Step 1: 创建 pyproject.toml**

```toml
[project]
name = "game-agent-platform"
version = "2.0.0"
description = "Multi-tenant game player behavior analysis and prediction platform"
requires-python = ">=3.11"
dependencies = [
    # Core
    "fastapi>=0.128.0",
    "uvicorn[standard]>=0.34.0",
    "pydantic>=2.10.0",
    "pydantic-settings>=2.7.0",

    # Database
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",

    # Redis
    "redis[hiredis]>=5.2.0",

    # Neo4j
    "neo4j>=5.27.0",

    # LangGraph + LangChain
    "langgraph>=1.0.0",
    "langgraph-checkpoint-postgres>=2.0.0",
    "langchain>=0.3.0",
    "langchain-openai>=0.3.0",

    # LightRAG
    "lightrag-hku>=1.4.10",

    # Prefect
    "prefect>=3.4.0",

    # Utilities
    "python-dotenv>=1.0.0",
    "httpx>=0.28.0",
    "sse-starlette>=2.2.0",
    "tenacity>=9.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "ruff>=0.8.0",
    "mypy>=1.13.0",
    "locust>=2.32.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "SIM"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
```

- [ ] **Step 2: 创建 docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: gameagent
      POSTGRES_PASSWORD: gameagent_secret
      POSTGRES_DB: gameagent
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U gameagent"]
      interval: 5s
      timeout: 5s
      retries: 5

  neo4j:
    image: neo4j:5-community
    environment:
      NEO4J_AUTH: neo4j/neo4j_secret
      NEO4J_PLUGINS: '["apoc"]'
    ports:
      - "7474:7474"
      - "7687:7687"
    volumes:
      - neo4j_data:/data
      - neo4j_logs:/logs
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p neo4j_secret 'RETURN 1'"]
      interval: 10s
      timeout: 10s
      retries: 10

  redis:
    image: redis:8-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  prefect-server:
    image: prefecthq/prefect:3-server
    ports:
      - "4200:4200"
    environment:
      PREFECT_SERVER_API_HOST: 0.0.0.0
      PREFECT_SERVER_API_PORT: 4200
    depends_on:
      postgres:
        condition: service_healthy

  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      POSTGRES_DSN: postgresql+asyncpg://gameagent:gameagent_secret@postgres:5432/gameagent
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USERNAME: neo4j
      NEO4J_PASSWORD: neo4j_secret
      REDIS_URL: redis://redis:6379/0
    depends_on:
      postgres:
        condition: service_healthy
      neo4j:
        condition: service_healthy
      redis:
        condition: service_healthy

volumes:
  postgres_data:
  neo4j_data:
  neo4j_logs:
  redis_data:
```

- [ ] **Step 3: 创建 .env.example**

```bash
# ============================================
# Game Agent Platform v2 - Environment Variables
# ============================================

# LLM Configuration
LLM_PROVIDER=deepseek
OPENAI_API_KEY=sk-your-key
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_DEFAULT_MODEL=deepseek-chat
OPENAI_FAST_MODEL=deepseek-chat

# Database
POSTGRES_DSN=postgresql+asyncpg://gameagent:gameagent_secret@localhost:5432/gameagent

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=neo4j_secret
NEO4J_DATABASE=neo4j

# Redis
REDIS_URL=redis://localhost:6379/0

# Game Database (read-only)
GAME_DB_DSN=postgresql+asyncpg://readonly:password@game-db-host/gamedb

# RAG
RAG_DEFAULT_STRATEGY=hybrid
RAG_WORKING_DIR=./rag_storage

# Scheduler
MAX_CONCURRENT_ANALYSES=20
OFFLINE_TRIGGER_MINUTES=5

# Token Quota
DEFAULT_MONTHLY_TOKENS=40000000
QUOTA_WARNING_THRESHOLD=0.8
```

- [ ] **Step 4: 创建 .gitignore**

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.eggs/

# Virtual environments
.venv/
venv/

# Environment
.env

# IDE
.vscode/
.idea/
*.swp
*.swo

# RAG storage (local cache, Neo4j is source of truth)
rag_storage/

# Models
models/

# Logs
logs/
*.log

# Testing
.pytest_cache/
.ruff_cache/
.mypy_cache/
coverage/
htmlcov/

# OS
.DS_Store
Thumbs.db

# Data
data/
```

- [ ] **Step 5: 创建所有 __init__.py 空文件**

```bash
# 为以下目录创建空的 __init__.py:
# src/, src/core/, src/core/infrastructure/, src/core/llm/,
# src/core/engine/, src/core/agents/, src/core/output/,
# src/core/scheduler/, src/api/, src/api/routes/, src/game_specific/
```

- [ ] **Step 6: 验证项目结构**

```bash
# 确认目录结构正确
find src -type f -name "*.py" | sort
```

---

### Task 2: Pydantic Settings 统一配置

**Files:**
- Create: `src/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_config.py
import pytest
from src.config import Settings

def test_settings_defaults(monkeypatch):
    """测试默认配置加载"""
    # 清除所有环境变量
    for key in [
        "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENAI_DEFAULT_MODEL", "POSTGRES_DSN", "NEO4J_URI",
        "NEO4J_USERNAME", "NEO4J_PASSWORD", "REDIS_URL",
    ]:
        monkeypatch.delenv(key, raising=False)

    # 设置必需的最小配置
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+asyncpg://test:test@localhost/test")

    settings = Settings()
    assert settings.llm_provider == "deepseek"
    assert settings.neo4j_uri == "bolt://localhost:7687"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.offline_trigger_minutes == 5
    assert settings.max_concurrent_analyses == 20
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_config.py -v
# Expected: ModuleNotFoundError (config.py doesn't exist yet)
```

- [ ] **Step 3: 实现配置**

```python
# src/config.py
"""Pydantic Settings 统一配置管理"""

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，从环境变量和 .env 文件加载"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── LLM 配置 ──
    llm_provider: str = Field(default="deepseek", description="LLM 提供商标识")
    openai_api_key: str = Field(..., description="OpenAI 兼容 API Key")
    openai_base_url: str = Field(..., description="OpenAI 兼容 API Base URL")
    openai_default_model: str = Field(default="deepseek-chat", description="主力模型")
    openai_fast_model: str = Field(default="deepseek-chat", description="快速模型")

    # ── 数据库 ──
    postgres_dsn: PostgresDsn = Field(..., description="PostgreSQL 连接字符串")
    neo4j_uri: str = Field(default="bolt://localhost:7687", description="Neo4j 连接 URI")
    neo4j_username: str = Field(default="neo4j", description="Neo4j 用户名")
    neo4j_password: str = Field(default="neo4j_secret", description="Neo4j 密码")
    neo4j_database: str = Field(default="neo4j", description="Neo4j 数据库名")

    # ── Redis ──
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis 连接字符串")

    # ── 游戏数据库 ──
    game_db_dsn: PostgresDsn | None = Field(default=None, description="游戏数据库只读连接")

    # ── RAG ──
    rag_default_strategy: str = Field(default="hybrid", description="默认 RAG 检索策略")
    rag_working_dir: str = Field(default="./rag_storage", description="LightRAG 工作目录")

    # ── 调度 ──
    max_concurrent_analyses: int = Field(default=20, ge=1, le=100, description="最大并发分析数")
    offline_trigger_minutes: int = Field(default=5, ge=1, description="离线触发防抖时间（分钟）")

    # ── Token 配额 ──
    default_monthly_tokens: int = Field(default=40_000_000, description="默认月度 Token 配额")
    quota_warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0, description="配额警告阈值")


# 全局单例
settings = Settings()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_config.py -v
# Expected: PASS
```

---

### Task 3: SQLAlchemy async 数据库层

**Files:**
- Create: `src/core/infrastructure/db.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/001_initial.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: 实现 SQLAlchemy async 引擎**

```python
# src/core/infrastructure/db.py
"""SQLAlchemy 异步数据库引擎和会话管理"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

# 创建异步引擎
engine = create_async_engine(
    str(settings.postgres_dsn),
    echo=False,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=40,
    pool_recycle=3600,
)

# 异步会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话的异步上下文管理器"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """初始化数据库连接（启动时调用）"""
    async with engine.begin() as conn:
        # 测试连接
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))


async def close_db() -> None:
    """关闭数据库连接（关闭时调用）"""
    await engine.dispose()
```

- [ ] **Step 2: 配置 Alembic**

```ini
# alembic.ini
[alembic]
script_location = alembic
sqlalchemy.url = postgresql+asyncpg://gameagent:gameagent_secret@localhost:5432/gameagent

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

```python
# alembic/env.py
"""Alembic 异步迁移环境"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.config import settings

config = context.config

# 覆盖 sqlalchemy.url
config.set_main_option("sqlalchemy.url", str(settings.postgres_dsn).replace("+asyncpg", ""))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入所有模型（用于 autogenerate）
# from src.core.output.schema import *  # noqa
# from src.game_specific.models import *  # noqa

target_metadata = None  # 后续添加模型后更新


def run_migrations_off() -> None:
    context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_off()
else:
    run_migrations_online()
```

- [ ] **Step 3: 创建初始迁移**

```python
# alembic/versions/001_initial.py
"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 租户表
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.String(255), nullable=False, unique=True),
        sa.Column("api_key", sa.String(255), nullable=False, unique=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_tenants_user_id", "tenants", ["user_id"])
    op.create_index("ix_tenants_api_key", "tenants", ["api_key"])

    # Token 配额表
    op.create_table(
        "quotas",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("monthly_limit", sa.BigInteger(), nullable=False),
        sa.Column("used", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "period_start", name="uq_quotas_tenant_period"),
    )
    op.create_index("ix_quotas_tenant_id", "quotas", ["tenant_id"])

    # 分析结果表
    op.create_table(
        "analysis_results",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_analysis_results_tenant_user", "analysis_results", ["tenant_id", "user_id"])
    op.create_index("ix_analysis_results_user_id", "analysis_results", ["user_id"])

    # LangGraph 检查点表（由 PostgresSaver 使用）
    # 注意：PostgresSaver.setup() 会自动创建这些表，此处不手动创建


def downgrade() -> None:
    op.drop_table("analysis_results")
    op.drop_table("quotas")
    op.drop_table("tenants")
```

- [ ] **Step 4: 编写数据库层测试**

```python
# tests/test_db.py
"""数据库层测试"""

import pytest
from sqlalchemy import text
from src.core.infrastructure.db import get_session, init_db, close_db


@pytest.mark.asyncio
async def test_init_db():
    """测试数据库连接初始化"""
    await init_db()
    await close_db()


@pytest.mark.asyncio
async def test_get_session():
    """测试会话管理"""
    async with get_session() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
```

- [ ] **Step 5: 运行测试**

```bash
# 确保 PostgreSQL 运行
docker compose up -d postgres

# 运行迁移
alembic upgrade head

# 运行测试
pytest tests/test_db.py -v
```

---

### Task 4: Redis 异步连接

**Files:**
- Create: `src/core/infrastructure/redis.py`

- [ ] **Step 1: 实现 Redis 连接池**

```python
# src/core/infrastructure/redis.py
"""Redis 异步连接池"""

import redis.asyncio as redis

from src.config import settings

# 全局 Redis 连接池
redis_pool: redis.Redis | None = None


async def init_redis() -> redis.Redis:
    """初始化 Redis 连接"""
    global redis_pool
    redis_pool = redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    # 测试连接
    await redis_pool.ping()
    return redis_pool


async def get_redis() -> redis.Redis:
    """获取 Redis 连接"""
    if redis_pool is None:
        return await init_redis()
    return redis_pool


async def close_redis() -> None:
    """关闭 Redis 连接"""
    global redis_pool
    if redis_pool:
        await redis_pool.aclose()
        redis_pool = None
```

---

### Task 5: Neo4j 连接

**Files:**
- Create: `src/core/infrastructure/neo4j.py`

- [ ] **Step 1: 实现 Neo4j 驱动**

```python
# src/core/infrastructure/neo4j.py
"""Neo4j 异步驱动"""

from neo4j import AsyncGraphDatabase
from neo4j.asyncio import AsyncDriver

from src.config import settings

_driver: AsyncDriver | None = None


async def init_neo4j() -> AsyncDriver:
    """初始化 Neo4j 驱动"""
    global _driver
    _driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
        max_connection_pool_size=50,
    )
    # 测试连接
    async with _driver.session(database=settings.neo4j_database) as session:
        await session.run("RETURN 1")
    return _driver


def get_neo4j_driver() -> AsyncDriver:
    """获取 Neo4j 驱动"""
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialized. Call init_neo4j() first.")
    return _driver


async def close_neo4j() -> None:
    """关闭 Neo4j 驱动"""
    global _driver
    if _driver:
        await _driver.close()
        _driver = None
```

---

### Task 6: 熔断器 + 重试机制

**Files:**
- Create: `src/core/infrastructure/resilience.py`

- [ ] **Step 1: 实现熔断器和重试**

```python
# src/core/infrastructure/resilience.py
"""熔断器模式和重试机制"""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from functools import wraps
from typing import Any

from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class CircuitBreakerOpen(Exception):
    """熔断器打开异常"""

    pass


class CircuitBreaker:
    """熔断器实现"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._last_failure_time = 0.0
        self._state = "closed"  # closed, open, half-open

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = "half-open"
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = "open"
            logger.warning(f"[WARN] Circuit breaker '{self.name}' opened after {self._failures} failures")

    async def execute(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        if self.state == "open":
            raise CircuitBreakerOpen(f"Circuit breaker '{self.name}' is open")

        try:
            result = await func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure()
            raise


# 全局熔断器注册表
_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str) -> CircuitBreaker:
    """获取或创建熔断器"""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    """异步重试装饰器"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=base_delay, max=max_delay),
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)

        return wrapper

    return decorator
```

---

## Chunk 2: LLM 层 + RAG 引擎

### Task 7: LLM 客户端工厂

**Files:**
- Create: `src/core/llm/factory.py`
- Create: `src/core/llm/quota.py`

- [ ] **Step 1: 实现 LLM 工厂**

```python
# src/core/llm/factory.py
"""多提供商 LLM 客户端工厂"""

from langchain_openai import ChatOpenAI

from src.config import settings

# LLM 实例缓存
_llm_cache: dict[str, ChatOpenAI] = {}


def get_llm(model_type: str = "default", temperature: float = 0.0) -> ChatOpenAI:
    """
    获取 LLM 实例

    Args:
        model_type: "default"（主力模型）或 "fast"（快速模型）
        temperature: 温度参数
    """
    cache_key = f"{model_type}_{temperature}"
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    model = (
        settings.openai_default_model if model_type == "default" else settings.openai_fast_model
    )

    llm = ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        streaming=False,
        max_retries=2,
    )

    _llm_cache[cache_key] = llm
    return llm


def get_llm_with_reasoning(model_type: str = "default") -> ChatOpenAI:
    """获取支持推理的 LLM 实例（extended thinking）"""
    return get_llm(model_type=model_type, temperature=0.0)
```

- [ ] **Step 2: 实现 Token 配额跟踪**

```python
# src/core/llm/quota.py
"""Token 配额跟踪"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token 使用记录"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    last_request: datetime | None = None


class TokenUsageTracker:
    """Token 使用跟踪器（内存中，定期同步到数据库）"""

    def __init__(self):
        self._usages: dict[str, TokenUsage] = {}

    def record(self, tenant_id: str, usage: TokenUsage) -> None:
        if tenant_id not in self._usages:
            self._usages[tenant_id] = TokenUsage()
        existing = self._usages[tenant_id]
        existing.prompt_tokens += usage.prompt_tokens
        existing.completion_tokens += usage.completion_tokens
        existing.total_tokens += usage.total_tokens
        existing.requests += 1
        existing.last_request = datetime.now(timezone.utc)

    def get_usage(self, tenant_id: str) -> TokenUsage:
        return self._usages.get(tenant_id, TokenUsage())

    def reset(self, tenant_id: str) -> None:
        self._usages.pop(tenant_id, None)


# 全局跟踪器
token_tracker = TokenUsageTracker()


class TokenUsageCallback(BaseCallbackHandler):
    """LangChain Token 使用回调"""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        if response.llm_output and "token_usage" in response.llm_output:
            usage = response.llm_output["token_usage"]
            token_tracker.record(
                self.tenant_id,
                TokenUsage(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    requests=1,
                    last_request=datetime.now(timezone.utc),
                ),
            )
```

---

### Task 8: LightRAG 封装

**Files:**
- Create: `src/core/engine/rag.py`

- [ ] **Step 1: 实现 LightRAG 封装**

```python
# src/core/engine/rag.py
"""LightRAG 封装 — 知识图谱 RAG 引擎"""

import logging
from enum import Enum

import numpy as np
from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc, setup_logger, wrap_embedding_func_with_attrs

from src.config import settings

logger = logging.getLogger(__name__)


class RetrievalStrategy(str, Enum):
    """RAG 检索策略"""

    NAIVE = "naive"
    LOCAL = "local"
    GLOBAL = "global"
    HYBRID = "hybrid"
    MIX = "mix"


# BGE-M3 embedding 维度
EMBEDDING_DIM = 1024


@wrap_embedding_func_with_attrs(embedding_dim=EMBEDDING_DIM, max_token_size=8192)
async def bge_m3_embed(texts: list[str]) -> np.ndarray:
    """
    BGE-M3 embedding 函数

    注意：如果有本地 BGE-M3 模型，替换为本地推理。
    这里使用 OpenAI 兼容接口作为占位。
    """
    return await openai_embed.func(texts, model="text-embedding-3-small")


class GameRuleRAG:
    """游戏规则 RAG 引擎（基于 LightRAG）"""

    def __init__(
        self,
        working_dir: str | None = None,
        strategy: RetrievalStrategy = RetrievalStrategy.HYBRID,
    ):
        self.working_dir = working_dir or settings.rag_working_dir
        self.strategy = strategy
        self._rag: LightRAG | None = None

    async def initialize(self) -> LightRAG:
        """初始化 LightRAG 实例"""
        if self._rag is not None:
            return self._rag

        setup_logger("lightrag", level="INFO")

        self._rag = LightRAG(
            working_dir=self.working_dir,
            llm_model_func=openai_complete_if_cache,
            llm_model_name=settings.openai_default_model,
            llm_model_kwargs={
                "api_key": settings.openai_api_key,
                "base_url": settings.openai_base_url,
            },
            embedding_func=EmbeddingFunc(
                embedding_dim=EMBEDDING_DIM,
                max_token_size=8192,
                func=lambda texts: bge_m3_embed(texts),
            ),
            graph_storage="Neo4JStorage",
        )

        await self._rag.initialize_storages()
        logger.info("[INFO] LightRAG initialized with Neo4JStorage")
        return self._rag

    async def insert(self, document: str, doc_id: str | None = None) -> None:
        """
        文档入库，自动构建知识图谱

        流程：分块 → 实体提取 → 关系构建 → 存入 Neo4j
        """
        rag = await self.initialize()
        await rag.ainsert(document)
        logger.info(f"[INFO] Document inserted: {doc_id or 'unknown'}")

    async def query(
        self,
        query: str,
        strategy: RetrievalStrategy | None = None,
        top_k: int = 8,
    ) -> str:
        """
        查询知识图谱

        Args:
            query: 查询文本
            strategy: 检索策略（默认使用实例策略）
            top_k: 返回结果数量
        """
        rag = await self.initialize()
        mode = (strategy or self.strategy).value

        result = await rag.aquery(
            query,
            param=QueryParam(mode=mode, top_k=top_k),
        )
        return result

    async def finalize(self) -> None:
        """关闭存储"""
        if self._rag:
            await self._rag.finalize_storages()
            self._rag = None


# 全局单例
_rag_instance: GameRuleRAG | None = None


def get_rag(strategy: RetrievalStrategy = RetrievalStrategy.HYBRID) -> GameRuleRAG:
    """获取 RAG 单例"""
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = GameRuleRAG(strategy=strategy)
    return _rag_instance
```

---

### Task 9: 行为分析器

**Files:**
- Create: `src/core/engine/behavior.py`

- [ ] **Step 1: 实现行为分析器**

```python
# src/core/engine/behavior.py
"""玩家行为分析器（快速模型）"""

import logging

from langchain_core.prompts import ChatPromptTemplate

from src.core.llm.factory import get_llm
from src.game_specific.models import PlayerSnapshot

logger = logging.getLogger(__name__)

BEHAVIOR_ANALYSIS_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个专业的游戏行为分析师。根据玩家快照数据，分析其游戏行为特征。

输出格式（JSON）：
{{
  "playstyle": "玩家风格（aggressive/defensive/explorer/collector/social/competitive）",
  "current_goal": "当前主要目标",
  "bottlenecks": ["遇到的瓶颈列表"],
  "resource_status": "资源状态（normal/abundant/scarce）",
  "play_time_pattern": "游戏时间模式",
  "engagement_level": "参与度（high/medium/low）"
}}"""),
    ("human", """分析以下玩家数据：

玩家快照：
{snapshot_text}

{rag_context}

请输出 JSON 格式的分析结果。"""),
])


async def analyze_behavior(
    snapshot: PlayerSnapshot,
    rag_context: str = "",
) -> str:
    """
    分析玩家行为

    Args:
        snapshot: 玩家快照
        rag_context: RAG 检索到的规则上下文

    Returns:
        JSON 格式的行为分析报告
    """
    llm = get_llm(model_type="fast", temperature=0.0)
    chain = BEHAVIOR_ANALYSIS_PROMPT | llm

    snapshot_text = snapshot.to_text() if hasattr(snapshot, "to_text") else str(snapshot.model_dump())

    response = await chain.ainvoke({
        "snapshot_text": snapshot_text,
        "rag_context": rag_context or "（无额外规则上下文）",
    })

    return response.content
```

---

## Chunk 3: Agent 层 + 输出层

### Task 10: LangGraph 状态定义

**Files:**
- Create: `src/core/agents/state.py`

- [ ] **Step 1: 定义所有状态**

```python
# src/core/agents/state.py
"""LangGraph Agent 状态定义"""

from typing import Any

from typing_extensions import TypedDict

from src.game_specific.models import PlayerSnapshot


class BehaviorState(TypedDict):
    """行为分析子图状态"""

    snapshot: PlayerSnapshot
    rag_context: str
    analysis: str  # JSON 格式的行为分析报告


class ReasonerState(TypedDict):
    """推理子图状态"""

    behavior_report: str
    snapshot: PlayerSnapshot
    rag_context: str
    actions: list[dict[str, Any]]  # 行动序列


class AnalysisState(TypedDict):
    """主协调图状态"""

    user_id: str
    tenant_id: str
    snapshot: PlayerSnapshot
    behavior_report: str
    reasoned_actions: list[dict[str, Any]]
    final_output: dict[str, Any]  # 最终 JSON 输出
    errors: list[str]
```

---

### Task 11: 行为分析子图

**Files:**
- Create: `src/core/agents/behavior_graph.py`
- Create: `src/core/agents/tools.py`
- Create: `src/core/agents/prompts.py`

- [ ] **Step 1: 实现行为分析子图**

```python
# src/core/agents/behavior_graph.py
"""行为分析子图 — 拉取快照 → RAG 检索 → LLM 分析"""

import logging

from langgraph.graph import END, START, StateGraph

from src.core.agents.state import BehaviorState
from src.core.agents.tools import retrieve_game_rules
from src.core.engine.behavior import analyze_behavior

logger = logging.getLogger(__name__)


async def retrieve_rules_node(state: BehaviorState) -> dict:
    """检索游戏规则"""
    snapshot = state["snapshot"]
    query = f"玩家行为分析：{snapshot.player_name or snapshot.user_id} 的行为规则"

    rag_context = await retrieve_game_rules(query)
    logger.info(f"[INFO] Retrieved {len(rag_context)} chars of rule context")
    return {"rag_context": rag_context}


async def analyze_node(state: BehaviorState) -> dict:
    """调用 LLM 分析行为"""
    analysis = await analyze_behavior(
        snapshot=state["snapshot"],
        rag_context=state["rag_context"],
    )
    logger.info(f"[INFO] Behavior analysis complete")
    return {"analysis": analysis}


def build_behavior_graph() -> StateGraph:
    """构建行为分析子图"""
    builder = StateGraph(BehaviorState)
    builder.add_node("retrieve_rules", retrieve_rules_node)
    builder.add_node("analyze", analyze_node)
    builder.add_edge(START, "retrieve_rules")
    builder.add_edge("retrieve_rules", "analyze")
    builder.add_edge("analyze", END)
    return builder.compile()


# 编译后的子图
behavior_graph = build_behavior_graph()
```

```python
# src/core/agents/tools.py
"""Agent 工具函数"""

import logging

from src.core.engine.rag import get_rag

logger = logging.getLogger(__name__)


async def retrieve_game_rules(query: str) -> str:
    """从 LightRAG 检索游戏规则"""
    rag = get_rag()
    try:
        result = await rag.query(query)
        return result
    except Exception as e:
        logger.error(f"[FAIL] RAG query failed: {e}")
        return f"RAG 检索失败: {e}"
```

```python
# src/core/agents/prompts.py
"""Agent Prompt 模板"""

# 从 prompts.yaml 加载（如果存在），否则使用默认值
# 这里提供默认 prompt，后续可迁移到 YAML

ORCHESTRATOR_SYSTEM_PROMPT = """你是游戏玩家行为分析平台的协调者。
你的职责是：
1. 获取玩家快照数据
2. 委托行为分析子 Agent 分析玩家行为
3. 委托推理子 Agent 生成行动建议
4. 合并输出为结构化 JSON"""
```

---

### Task 12: 推理子图

**Files:**
- Create: `src/core/agents/reasoner_graph.py`

- [ ] **Step 1: 实现推理子图**

```python
# src/core/agents/reasoner_graph.py
"""推理子图 — RAG 检索 → 深度推理 → 行动序列"""

import logging

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

from src.core.agents.state import ReasonerState
from src.core.agents.tools import retrieve_game_rules
from src.core.llm.factory import get_llm_with_reasoning

logger = logging.getLogger(__name__)

REASONING_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个资深的游戏策略推理专家。根据玩家行为分析报告和游戏规则，推理出最优行动方案。

输出格式（JSON 数组）：
[
  {{
    "action_type": "preparation|combat|exploration|social|quest",
    "target": "具体行动目标",
    "priority": 1-10,
    "confidence": 0.0-1.0,
    "reasoning": "推理过程",
    "rule_source": "规则来源",
    "estimated_duration_minutes": 预计时长,
    "prerequisites": ["前置条件"]
  }}
]"""),
    ("human", """行为分析报告：
{behavior_report}

{rag_context}

请推理出最优的行动序列，输出 JSON 数组。"""),
])


async def retrieve_rules_node(state: ReasonerState) -> dict:
    """检索相关游戏规则"""
    # 从行为报告中提取关键词进行检索
    query = f"游戏策略和行动建议"
    rag_context = await retrieve_game_rules(query)
    return {"rag_context": rag_context}


async def reason_node(state: ReasonerState) -> dict:
    """深度推理"""
    llm = get_llm_with_reasoning()
    chain = REASONING_PROMPT | llm

    response = await chain.ainvoke({
        "behavior_report": state["behavior_report"],
        "rag_context": state["rag_context"],
    })

    return {"actions": response.content}


def build_reasoner_graph() -> StateGraph:
    """构建推理子图"""
    builder = StateGraph(ReasonerState)
    builder.add_node("retrieve_rules", retrieve_rules_node)
    builder.add_node("reason", reason_node)
    builder.add_edge(START, "retrieve_rules")
    builder.add_edge("retrieve_rules", "reason")
    builder.add_edge("reason", END)
    return builder.compile()


reasoner_graph = build_reasoner_graph()
```

---

### Task 13: 主协调图

**Files:**
- Create: `src/core/agents/orchestrator.py`

- [ ] **Step 1: 实现主协调图**

```python
# src/core/agents/orchestrator.py
"""主协调图 — Orchestrator"""

import logging
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from src.core.agents.behavior_graph import behavior_graph
from src.core.agents.reasoner_graph import reasoner_graph
from src.core.agents.state import AnalysisState
from src.core.output.schema import PlayerAnalysisOutput

logger = logging.getLogger(__name__)


async def fetch_snapshot_node(state: AnalysisState) -> dict:
    """从游戏数据库获取玩家快照"""
    from src.game_specific.connector import fetch_player_snapshot

    snapshot = await fetch_player_snapshot(state["user_id"])
    logger.info(f"[INFO] Fetched snapshot for user {state['user_id']}")
    return {"snapshot": snapshot}


async def behavior_analysis_node(state: AnalysisState) -> dict:
    """调用行为分析子图"""
    result = await behavior_graph.ainvoke({
        "snapshot": state["snapshot"],
        "rag_context": "",
        "analysis": "",
    })
    return {"behavior_report": result["analysis"]}


async def action_reasoning_node(state: AnalysisState) -> dict:
    """调用推理子图"""
    result = await reasoner_graph.ainvoke({
        "behavior_report": state["behavior_report"],
        "snapshot": state["snapshot"],
        "rag_context": "",
        "actions": [],
    })
    return {"reasoned_actions": result["actions"]}


async def merge_output_node(state: AnalysisState) -> dict:
    """合并输出为结构化 JSON"""
    import json

    try:
        behavior = json.loads(state["behavior_report"]) if isinstance(state["behavior_report"], str) else state["behavior_report"]
        actions = json.loads(state["reasoned_actions"]) if isinstance(state["reasoned_actions"], str) else state["reasoned_actions"]

        output = PlayerAnalysisOutput(
            user_id=state["user_id"],
            analyzed_at=datetime.now(timezone.utc),
            player_profile=behavior,
            recommended_actions=actions,
        )
        return {"final_output": output.model_dump(mode="json")}
    except Exception as e:
        logger.error(f"[FAIL] Failed to merge output: {e}")
        return {"errors": state["errors"] + [str(e)]}


def build_orchestrator() -> StateGraph:
    """构建主协调图"""
    builder = StateGraph(AnalysisState)

    builder.add_node("fetch_snapshot", fetch_snapshot_node)
    builder.add_node("behavior_analysis", behavior_analysis_node)
    builder.add_node("action_reasoning", action_reasoning_node)
    builder.add_node("merge_output", merge_output_node)

    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "behavior_analysis")
    builder.add_edge("behavior_analysis", "action_reasoning")
    builder.add_edge("action_reasoning", "merge_output")
    builder.add_edge("merge_output", END)

    return builder.compile()


# 编译后的主图
orchestrator = build_orchestrator()
```

---

### Task 14: 输出 Schema

**Files:**
- Create: `src/core/output/schema.py`

- [ ] **Step 1: 定义 Pydantic 输出模型**

```python
# src/core/output/schema.py
"""PlayerAnalysisOutput — Pydantic v2 结构化输出模型"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReanalysisTriggers(BaseModel):
    """重新分析触发条件"""

    model_config = ConfigDict(strict=True)

    on_level_up: bool = Field(default=True, description="升级时触发")
    on_quest_complete: bool = Field(default=True, description="任务完成时触发")
    after_hours: int = Field(default=8, description="N 小时后重新分析")


class PlayerAnalysisOutput(BaseModel):
    """玩家分析完整输出"""

    model_config = ConfigDict(strict=True)

    schema_version: str = Field(default="2.0")
    user_id: str = Field(..., description="玩家 ID")
    analyzed_at: datetime = Field(..., description="分析时间")
    player_profile: dict[str, Any] = Field(..., description="玩家画像")
    recommended_actions: list[dict[str, Any]] = Field(..., description="推荐行动序列")
    overall_confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="整体置信度")
    analysis_notes: str | None = Field(default=None, description="分析备注")
    reanalysis_triggers: ReanalysisTriggers = Field(default_factory=ReanalysisTriggers)
```

---

### Task 15: Agent 测试

**Files:**
- Create: `tests/test_agents.py`

- [ ] **Step 1: 编写 Agent 图测试**

```python
# tests/test_agents.py
"""Agent 图测试"""

import pytest
from src.core.agents.state import AnalysisState, BehaviorState, ReasonerState


def test_behavior_state():
    """测试行为分析状态定义"""
    state: BehaviorState = {
        "snapshot": None,
        "rag_context": "",
        "analysis": "",
    }
    assert state["rag_context"] == ""


def test_reasoner_state():
    """测试推理状态定义"""
    state: ReasonerState = {
        "behavior_report": "",
        "snapshot": None,
        "rag_context": "",
        "actions": [],
    }
    assert state["actions"] == []


def test_analysis_state():
    """测试主协调状态定义"""
    state: AnalysisState = {
        "user_id": "test_player",
        "tenant_id": "test_tenant",
        "snapshot": None,
        "behavior_report": "",
        "reasoned_actions": [],
        "final_output": {},
        "errors": [],
    }
    assert state["errors"] == []
```

---

## Chunk 4: API 层 + 调度层

### Task 16: FastAPI 应用入口 + 中间件

**Files:**
- Create: `src/api/main.py`
- Create: `src/api/middleware.py`

- [ ] **Step 1: 实现中间件**

```python
# src/api/middleware.py
"""FastAPI 中间件：认证 + 限流 + 配额"""

import logging
import time

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import settings

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """API Key 认证中间件"""

    # 不需要认证的路径
    EXCLUDED_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(status_code=401, content={"detail": "Missing X-API-Key header"})

        # 将 tenant 信息存入 state，供后续路由使用
        # 实际验证逻辑在路由中完成
        request.state.api_key = api_key
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Redis 滑动窗口限流"""

    def __init__(self, app, max_requests: int = 100, window: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in {"/health", "/docs", "/openapi.json"}:
            return await call_next(request)

        # 简化版：内存计数（生产环境切换到 Redis）
        client_ip = request.client.host
        key = f"rate:{client_ip}"

        # TODO: 使用 Redis ZSET 实现滑动窗口
        # 当前使用简单计数
        return await call_next(request)
```

- [ ] **Step 2: 实现 FastAPI 入口**

```python
# src/api/main.py
"""FastAPI 应用入口"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.middleware import AuthMiddleware, RateLimitMiddleware
from src.api.routes import analysis, quota, tenants, webhooks
from src.config import settings
from src.core.infrastructure.db import close_db, init_db
from src.core.infrastructure.neo4j import close_neo4j, init_neo4j
from src.core.infrastructure.redis import close_redis, init_redis

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info("[INFO] Starting up...")
    await init_db()
    await init_redis()
    await init_neo4j()
    logger.info("[OK] All services initialized")

    yield

    # 关闭
    logger.info("[INFO] Shutting down...")
    await close_db()
    await close_redis()
    await close_neo4j()
    logger.info("[OK] All services closed")


app = FastAPI(
    title="Game Agent Platform",
    version="2.0.0",
    description="多租户游戏玩家行为分析与预测平台",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 中间件
app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware, max_requests=100, window=60)

# 路由
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(analysis.router, prefix="/api/v1/analysis", tags=["analysis"])
app.include_router(tenants.router, prefix="/api/v1/tenants", tags=["tenants"])
app.include_router(quota.router, prefix="/api/v1/quota", tags=["quota"])


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "version": "2.0.0"}
```

---

### Task 17: API 路由

**Files:**
- Create: `src/api/routes/webhooks.py`
- Create: `src/api/routes/analysis.py`
- Create: `src/api/routes/tenants.py`
- Create: `src/api/routes/quota.py`

- [ ] **Step 1: 实现 Webhook 路由**

```python
# src/api/routes/webhooks.py
"""游戏服务器 Webhook"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class PlayerEvent(BaseModel):
    user_id: str
    event_type: str  # "online" | "offline"
    timestamp: float


@router.post("/player-event")
async def handle_player_event(event: PlayerEvent, request: Request):
    """处理玩家在线/离线事件"""
    logger.info(f"[INFO] Player event: {event.event_type} for {event.user_id}")

    if event.event_type == "offline":
        # 触发分析流程（通过 Prefect）
        # TODO: 调用 Prefect Flow
        pass
    elif event.event_type == "online":
        # 取消待分析任务
        # TODO: 取消逻辑
        pass
    else:
        raise HTTPException(status_code=400, detail=f"Unknown event type: {event.event_type}")

    return {"status": "accepted", "user_id": event.user_id}
```

- [ ] **Step 2: 实现分析结果路由**

```python
# src/api/routes/analysis.py
"""分析结果端点"""

import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{user_id}/latest")
async def get_latest_analysis(user_id: str, request: Request):
    """获取最新分析结果"""
    # TODO: 从数据库查询
    return {"user_id": user_id, "status": "not_implemented"}


@router.get("/{user_id}/stream")
async def stream_analysis(user_id: str, request: Request):
    """SSE 流式推送分析结果"""
    # TODO: SSE 实现
    return {"user_id": user_id, "status": "not_implemented"}


@router.get("/{user_id}/live")
async def live_analysis(user_id: str, request: Request):
    """实时查看分析进度"""
    # TODO: 实时进度
    return {"user_id": user_id, "status": "not_implemented"}
```

- [ ] **Step 3: 实现租户管理路由**

```python
# src/api/routes/tenants.py
"""租户管理端点"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.core.infrastructure.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterRequest(BaseModel):
    user_id: str


@router.post("/register")
async def register_tenant(req: RegisterRequest):
    """注册新租户"""
    tenant_id = str(uuid.uuid4())
    api_key = f"gap_{uuid.uuid4().hex[:24]}"

    # TODO: 存入数据库
    # TODO: 创建默认配额

    return {
        "tenant_id": tenant_id,
        "api_key": api_key,
        "user_id": req.user_id,
    }
```

- [ ] **Step 4: 实现配额管理路由**

```python
# src/api/routes/quota.py
"""配额管理端点"""

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/usage")
async def get_quota_usage(request: Request):
    """查看配额使用情况"""
    # TODO: 查询配额
    return {"status": "not_implemented"}
```

---

### Task 18: Prefect 调度

**Files:**
- Create: `src/core/scheduler/flows.py`
- Create: `src/core/scheduler/triggers.py`

- [ ] **Step 1: 实现 Prefect Flow**

```python
# src/core/scheduler/flows.py
"""Prefect Flow 定义"""

import logging
from uuid import UUID

from prefect import flow, task

logger = logging.getLogger(__name__)


@task(retries=2, retry_delay_seconds=10)
async def fetch_player_data(user_id: str) -> dict:
    """获取玩家数据"""
    from src.game_specific.connector import fetch_player_snapshot

    snapshot = await fetch_player_snapshot(user_id)
    return snapshot.model_dump(mode="json")


@task(retries=1, retry_delay_seconds=5)
async def run_analysis(user_id: str, snapshot_data: dict) -> dict:
    """运行 LangGraph 分析"""
    from src.core.agents.orchestrator import orchestrator

    result = await orchestrator.ainvoke({
        "user_id": user_id,
        "tenant_id": "",
        "snapshot": None,
        "behavior_report": "",
        "reasoned_actions": [],
        "final_output": {},
        "errors": [],
    })

    return result.get("final_output", {})


@task
async def store_result(user_id: str, result: dict) -> None:
    """存储分析结果"""
    from src.core.infrastructure.result_store import store_analysis

    await store_analysis(user_id, result)


@flow(name="player_offline_analysis_flow", version="2.0")
async def player_offline_analysis_flow(user_id: str):
    """玩家离线分析 Flow"""
    logger.info(f"[INFO] Starting analysis for {user_id}")

    snapshot_data = await fetch_player_data(user_id)
    result = await run_analysis(user_id, snapshot_data)
    await store_result(user_id, result)

    logger.info(f"[OK] Analysis complete for {user_id}")
    return result
```

- [ ] **Step 2: 实现触发器**

```python
# src/core/scheduler/triggers.py
"""离线检测触发器"""

import asyncio
import logging
from datetime import datetime, timedelta

from src.config import settings

logger = logging.getLogger(__name__)

# 防抖：记录待分析的玩家
_pending_analyses: dict[str, asyncio.TimerHandle] = {}


def schedule_analysis(user_id: str, callback) -> None:
    """
    调度分析任务（带防抖）

    如果玩家在 OFFLINE_TRIGGER_MINUTES 内重新上线，取消分析。
    """
    # 取消之前的调度
    if user_id in _pending_analyses:
        _pending_analyses[user_id].cancel()

    # 调度新的分析
    loop = asyncio.get_event_loop()
    handle = loop.call_later(
        settings.offline_trigger_minutes * 60,
        lambda: callback(user_id),
    )
    _pending_analyses[user_id] = handle
    logger.info(f"[INFO] Scheduled analysis for {user_id} in {settings.offline_trigger_minutes}min")


def cancel_analysis(user_id: str) -> None:
    """取消分析任务（玩家重新上线）"""
    if user_id in _pending_analyses:
        _pending_analyses[user_id].cancel()
        del _pending_analyses[user_id]
        logger.info(f"[INFO] Cancelled analysis for {user_id}")
```

---

## Chunk 5: 游戏特定层 + 测试 + 脚本

### Task 19: 游戏特定层

**Files:**
- Create: `src/game_specific/connector.py`
- Create: `src/game_specific/models.py`
- Create: `src/game_specific/ingest.py`

- [ ] **Step 1: 实现数据模型**

```python
# src/game_specific/models.py
"""PlayerSnapshot 数据模型"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PlayerSnapshot(BaseModel):
    """玩家快照 — 从游戏数据库获取的当前状态"""

    user_id: str = Field(..., description="玩家 ID")
    player_name: str | None = Field(default=None, description="玩家名称")
    level: int = Field(default=1, description="等级")
    guild: str | None = Field(default=None, description="公会")
    last_online: datetime | None = Field(default=None, description="最后在线时间")
    stats: dict[str, Any] = Field(default_factory=dict, description="额外统计数据")

    def to_text(self) -> str:
        """转换为文本（用于 LLM prompt）"""
        parts = [
            f"玩家ID: {self.user_id}",
            f"名称: {self.player_name or '未知'}",
            f"等级: {self.level}",
        ]
        if self.guild:
            parts.append(f"公会: {self.guild}")
        if self.last_online:
            parts.append(f"最后在线: {self.last_online.isoformat()}")
        if self.stats:
            parts.append(f"统计: {self.stats}")
        return "\n".join(parts)
```

- [ ] **Step 2: 实现连接器**

```python
# src/game_specific/connector.py
"""游戏数据库连接器"""

import logging

from sqlalchemy import text

from src.core.infrastructure.db import get_session
from src.game_specific.models import PlayerSnapshot

logger = logging.getLogger(__name__)


async def fetch_player_snapshot(user_id: str) -> PlayerSnapshot:
    """
    从游戏数据库获取玩家快照

    注意：修改 SQL 以适配你的游戏表结构。
    当 GAME_DB_DSN 包含 'game-db-host' 时返回 Mock 数据。
    """
    from src.config import settings

    # Mock 模式
    if settings.game_db_dsn and "game-db-host" in str(settings.game_db_dsn):
        logger.info("[INFO] Using mock player data")
        return PlayerSnapshot(
            user_id=user_id,
            player_name=f"MockPlayer_{user_id}",
            level=25,
            guild="测试公会",
            stats={
                "play_hours": 120,
                "quests_completed": 45,
                "pvp_wins": 30,
            },
        )

    # 真实数据库查询
    async with get_session() as session:
        row = await session.execute(
            text("""
                SELECT p.player_name, p.level, p.guild, p.last_online
                FROM players p
                WHERE p.user_id = :uid
            """),
            {"uid": user_id},
        )
        result = row.first()
        if not result:
            raise ValueError(f"Player {user_id} not found")

        return PlayerSnapshot(
            user_id=user_id,
            player_name=result[0],
            level=result[1],
            guild=result[2],
            stats={"raw": dict(result._mapping)},
        )
```

- [ ] **Step 3: 实现文档入库**

```python
# src/game_specific/ingest.py
"""游戏文档入库 — LightRAG"""

import asyncio
import logging
from pathlib import Path

from src.core.engine.rag import get_rag

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

GAME_DOCS_DIR = Path("game_docs")


async def ingest_documents() -> None:
    """将所有游戏文档入库到 LightRAG"""
    rag = get_rag()

    doc_files = list(GAME_DOCS_DIR.glob("*.md")) + list(GAME_DOCS_DIR.glob("*.txt"))
    if not doc_files:
        logger.warning("[WARN] No documents found in game_docs/")
        return

    for doc_path in doc_files:
        logger.info(f"[INFO] Ingesting: {doc_path.name}")
        content = doc_path.read_text(encoding="utf-8")
        await rag.insert(content, doc_id=doc_path.stem)

    logger.info(f"[OK] Ingested {len(doc_files)} documents")


def main():
    """CLI 入口"""
    asyncio.run(ingest_documents())


if __name__ == "__main__":
    main()
```

---

### Task 20: 结果持久化

**Files:**
- Create: `src/core/infrastructure/result_store.py`

- [ ] **Step 1: 实现结果存储**

```python
# src/core/infrastructure/result_store.py
"""分析结果持久化到 PostgreSQL"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.core.infrastructure.db import get_session

logger = logging.getLogger(__name__)


async def store_analysis(user_id: str, output: dict) -> None:
    """存储分析结果到 PostgreSQL"""
    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO analysis_results (tenant_id, user_id, snapshot_hash, output_json, analyzed_at)
                VALUES (:tenant_id, :user_id, :snapshot_hash, :output_json, :analyzed_at)
            """),
            {
                "tenant_id": None,  # TODO: 从上下文获取
                "user_id": user_id,
                "snapshot_hash": "placeholder",
                "output_json": output,
                "analyzed_at": datetime.now(timezone.utc),
            },
        )
        logger.info(f"[OK] Stored analysis result for {user_id}")
```

---

### Task 21: 管理脚本

**Files:**
- Create: `scripts/admin/switch_llm.py`
- Create: `scripts/admin/manage_api_keys.py`
- Create: `scripts/admin/manage_quota.py`
- Create: `scripts/tests/test_full.py`
- Create: `scripts/utils/check_env.py`

- [ ] **Step 1: 创建环境检查脚本**

```python
# scripts/utils/check_env.py
"""部署前配置检查"""

import os
import sys
from pathlib import Path

REQUIRED_VARS = [
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "POSTGRES_DSN",
]

OPTIONAL_VARS = [
    "GAME_DB_DSN",
    "NEO4J_URI",
    "REDIS_URL",
]

def check_env():
    """检查环境变量"""
    missing = []
    for var in REQUIRED_VARS:
        if not os.getenv(var):
            missing.append(var)

    if missing:
        print(f"[FAIL] Missing required environment variables: {', '.join(missing)}")
        print("[TIP] Copy .env.example to .env and fill in the values")
        sys.exit(1)

    print("[OK] All required environment variables are set")

    # 检查 .env 文件
    env_file = Path(".env")
    if env_file.exists():
        print(f"[OK] .env file exists")
    else:
        print("[WARN] .env file not found")

    # 检查 game_docs
    docs_dir = Path("game_docs")
    if docs_dir.exists():
        doc_count = len(list(docs_dir.glob("*.md")) + list(docs_dir.glob("*.txt")))
        print(f"[STATS] Found {doc_count} documents in game_docs/")
    else:
        print("[WARN] game_docs/ directory not found")

    print("[OK] Configuration check complete")

if __name__ == "__main__":
    check_env()
```

- [ ] **Step 2: 创建快速测试脚本**

```python
# scripts/tests/test_full.py
"""快速集成测试"""

import asyncio
import sys

async def main():
    print("[TEST] Starting platform tests...")

    # 测试 1: 配置加载
    try:
        from src.config import settings
        print(f"[OK] Config loaded: LLM provider = {settings.llm_provider}")
    except Exception as e:
        print(f"[FAIL] Config: {e}")
        return

    # 测试 2: 数据库连接
    try:
        from src.core.infrastructure.db import init_db, close_db
        await init_db()
        await close_db()
        print("[OK] Database connection")
    except Exception as e:
        print(f"[FAIL] Database: {e}")

    # 测试 3: RAG 初始化
    try:
        from src.core.engine.rag import get_rag
        rag = get_rag()
        await rag.initialize()
        await rag.finalize()
        print("[OK] RAG engine")
    except Exception as e:
        print(f"[FAIL] RAG: {e}")

    # 测试 4: Agent 图构建
    try:
        from src.core.agents.orchestrator import orchestrator
        print("[OK] Agent orchestrator graph")
    except Exception as e:
        print(f"[FAIL] Agent: {e}")

    # 测试 5: 输出 Schema
    try:
        from src.core.output.schema import PlayerAnalysisOutput
        print("[OK] Output schema")
    except Exception as e:
        print(f"[FAIL] Schema: {e}")

    print("[TEST] Tests complete")

if __name__ == "__main__":
    asyncio.run(main())
```

---

### Task 22: 输出 Schema 测试 + API 测试

**Files:**
- Create: `tests/test_output_schema.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: 编写输出 Schema 测试**

```python
# tests/test_output_schema.py
"""输出模型测试"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.core.output.schema import PlayerAnalysisOutput, ReanalysisTriggers


def test_reanalysis_triggers_defaults():
    """测试默认触发条件"""
    triggers = ReanalysisTriggers()
    assert triggers.on_level_up is True
    assert triggers.on_quest_complete is True
    assert triggers.after_hours == 8


def test_output_minimal():
    """测试最小输出"""
    output = PlayerAnalysisOutput(
        user_id="test_player",
        analyzed_at=datetime.now(timezone.utc),
        player_profile={"playstyle": "aggressive"},
        recommended_actions=[],
    )
    assert output.schema_version == "2.0"
    assert output.overall_confidence == 0.8
    assert output.analysis_notes is None


def test_output_full():
    """测试完整输出"""
    output = PlayerAnalysisOutput(
        user_id="player_001",
        analyzed_at=datetime.now(timezone.utc),
        player_profile={
            "playstyle": "aggressive",
            "current_goal": "击败巫妖王",
            "bottlenecks": ["装备不足"],
        },
        recommended_actions=[
            {
                "action_type": "preparation",
                "target": "购买圣水x5",
                "priority": 10,
                "confidence": 0.88,
                "reasoning": "需要圣水提升伤害",
                "rule_source": "boss_guide.md",
                "estimated_duration_minutes": 10,
                "prerequisites": [],
            }
        ],
        overall_confidence=0.88,
        analysis_notes="规则引用：boss_guide.md",
    )
    data = output.model_dump(mode="json")
    assert data["user_id"] == "player_001"
    assert len(data["recommended_actions"]) == 1
```

- [ ] **Step 2: 编写 API 集成测试**

```python
# tests/test_api.py
"""API 集成测试"""

import pytest
from fastapi.testclient import TestClient

from src.api.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check(client):
    """测试健康检查"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_docs(client):
    """测试 OpenAPI 文档可访问"""
    response = client.get("/docs")
    assert response.status_code == 200


def test_register_tenant(client):
    """测试租户注册"""
    response = client.post(
        "/api/v1/tenants/register",
        json={"user_id": "test_player_001"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "tenant_id" in data
    assert "api_key" in data
    assert data["api_key"].startswith("gap_")


def test_webhook_player_event(client):
    """测试玩家事件 webhook"""
    response = client.post(
        "/webhooks/player-event",
        json={"user_id": "test_player", "event_type": "offline", "timestamp": 1711000000},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_auth_missing_key(client):
    """测试缺少 API Key 返回 401"""
    response = client.get("/api/v1/analysis/test_player/latest")
    assert response.status_code == 401
```

---

### Task 23: RAG 测试

**Files:**
- Create: `tests/test_rag.py`

- [ ] **Step 1: 编写 RAG 测试**

```python
# tests/test_rag.py
"""RAG 引擎测试"""

import pytest

from src.core.engine.rag import GameRuleRAG, RetrievalStrategy


def test_retrieval_strategy_enum():
    """测试检索策略枚举"""
    assert RetrievalStrategy.HYBRID.value == "hybrid"
    assert RetrievalStrategy.LOCAL.value == "local"
    assert RetrievalStrategy.GLOBAL.value == "global"
    assert RetrievalStrategy.NAIVE.value == "naive"
    assert RetrievalStrategy.MIX.value == "mix"


@pytest.mark.asyncio
async def test_rag_initialization():
    """测试 RAG 初始化"""
    rag = GameRuleRAG()
    # 注意：此测试需要真实的 LLM API Key 和 Neo4j
    # 在没有基础设施时使用 mock
    assert rag.strategy == RetrievalStrategy.HYBRID
    assert rag.working_dir == "./rag_storage"
```

---

## 执行命令汇总

```bash
# 1. 安装依赖
uv sync

# 2. 启动基础设施
docker compose up -d postgres neo4j redis

# 3. 运行数据库迁移
alembic upgrade head

# 4. 运行测试
pytest tests/ -v

# 5. 快速集成测试
python scripts/tests/test_full.py

# 6. 入库游戏文档
uv run python -m src.game_specific.ingest

# 7. 启动 API 服务
uv run uvicorn src.api.main:app --reload --port 8000

# 8. 访问 API 文档
# http://localhost:8000/docs
```
