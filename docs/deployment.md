# 部署与运维指南

面向运维人员和接手项目的开发者。包含环境搭建、配置说明、数据库迁移、常用脚本和问题排查。

---

## 环境要求

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| Python | 3.11 - 3.12 | 推荐 3.12 |
| uv | 最新版 | Python 包管理器 |
| Docker | 20.10+ | 运行基础设施 |
| Docker Compose | v2+ | 编排多容器 |
| Git | 2.30+ | 版本控制 |

硬件建议（开发环境）：

| 资源 | 最低 | 推荐 |
|------|------|------|
| CPU | 2 核 | 4 核 |
| 内存 | 4 GB | 8 GB |
| 磁盘 | 10 GB | 20 GB |
| 网络 | 能访问 LLM API | - |

---

## 快速部署

### 1. 克隆项目

```bash
git clone https://gitee.com/Whoob/myAgent.git
cd myAgent
```

### 2. 安装 uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入以下**必填项**：

```bash
# LLM — 至少需要一个 API Key
OPENAI_API_KEY=sk-your-key              # DeepSeek 或 OpenAI
OPENAI_BASE_URL=https://api.deepseek.com

# Embedding — DashScope / Qwen
EMBEDDING_API_KEY=sk-your-dashscope-key

# Rerank — DashScope / Qwen
RERANK_API_KEY=sk-your-dashscope-key

# PostgreSQL — 与 docker-compose 保持一致
POSTGRES_DSN=postgresql+asyncpg://myagent:myagent@localhost:5432/myagent

# Neo4j — 与 docker-compose 保持一致
NEO4J_PASSWORD=myagent123
```

其余字段使用默认值即可。完整配置项见下方配置参考。

### 4. 启动基础设施

```bash
docker-compose -f docker-compose.dev.yml up -d
```

这会启动以下服务：

| 服务 | 端口 | 用途 |
|------|------|------|
| PostgreSQL | 5432 | 业务数据 |
| Redis | 6379 | 缓存/去重/限流 |
| Neo4j | 7474 (HTTP), 7687 (Bolt) | 知识图谱 |
| Milvus | 19530 (gRPC), 9091 (Metrics) | 向量存储 |
| etcd | 2379 | Milvus 元数据 |
| MinIO | 9000 (API), 9001 (Console) | Milvus 对象存储 |
| Prefect | 4200 | 调度（当前未使用） |

等待所有服务健康（约 30-60 秒）：

```bash
docker-compose -f docker-compose.dev.yml ps
```

所有服务状态为 `healthy` 后继续。

### 5. 安装 Python 依赖

```bash
uv sync
```

### 6. 数据库迁移

```bash
alembic upgrade head
```

这会创建 4 张表：`tenants`, `quotas`, `analysis_results`, `llm_providers`。

### 7. 初始化种子数据（可选）

```bash
# 创建测试租户和 LLM 提供商
uv run python scripts/seed_data.py
uv run python scripts/seed_provider.py
```

### 8. 启动服务

```bash
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

服务默认监听 `http://localhost:8000`。

验证启动成功：

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "2.0.0"}
```

访问交互式 API 文档：`http://localhost:8000/docs`

---

## 配置参考

所有配置通过 `.env` 文件管理。`src/config.py` 使用 `pydantic-settings` 加载。

### 应用配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `ENV` | string | `development` | 运行环境 |
| `LOG_LEVEL` | string | `INFO` | 日志级别: DEBUG/INFO/WARNING/ERROR |
| `APP_WORKERS` | int | `1` | Uvicorn 工作进程数 |
| `CORS_ALLOWED_ORIGINS` | string | `["http://localhost:8000"]` | CORS 白名单 (JSON 数组) |

### LLM 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `LLM_PROVIDER` | string | `deepseek` | 默认提供商名称 |
| `OPENAI_API_KEY` | string | **必填** | LLM API 密钥 |
| `OPENAI_BASE_URL` | string | **必填** | LLM API 地址 |
| `OPENAI_DEFAULT_MODEL` | string | `deepseek-chat` | 主力模型（用于行动推理） |
| `OPENAI_FAST_MODEL` | string | `deepseek-chat` | 快速模型（用于上下文收集和行为分析） |

### Embedding 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `EMBEDDING_API_KEY` | string | **必填** | DashScope API Key |
| `EMBEDDING_BASE_URL` | string | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Embedding API 地址 |
| `EMBEDDING_MODEL` | string | `text-embedding-v4` | Qwen Embedding 模型 |
| `EMBEDDING_DIM` | int | `1024` | 向量维度 |

### Rerank 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `RERANK_API_KEY` | string | **必填** | DashScope API Key |
| `RERANK_MODEL` | string | `gte-rerank-v2` | Rerank 模型 |

### PostgreSQL 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `POSTGRES_DSN` | string | **必填** | 连接字符串，格式: `postgresql+asyncpg://user:pass@host:port/db` |

Docker Compose 默认: `postgresql+asyncpg://myagent:myagent@localhost:5432/myagent`

### Neo4j 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `NEO4J_URI` | string | `bolt://localhost:7687` | 连接地址 |
| `NEO4J_USERNAME` | string | `neo4j` | 用户名 |
| `NEO4J_PASSWORD` | string | **必填** | 密码 |
| `NEO4J_DATABASE` | string | `neo4j` | 数据库名 |

Docker Compose 默认密码: `myagent123`

### Redis 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `REDIS_URL` | string | `redis://localhost:6379/0` | 连接地址 |

Docker Compose 默认: `redis://:myagent@localhost:6379/0`

### Milvus 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `MILVUS_URI` | string | `http://localhost:19530` | 连接地址 |
| `MILVUS_USER` | string | `root` | 用户名 |
| `MILVUS_PASSWORD` | string | (空) | 密码 |
| `MILVUS_DB_NAME` | string | `lightrag` | 数据库名 |

### 游戏数据库

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `GAME_DB_DSN` | string | `null` | 游戏数据库连接（可选，用于主动拉取玩家数据） |

### RAG 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `RAG_DEFAULT_STRATEGY` | string | `hybrid` | 检索策略: local/global/hybrid/naive |
| `RAG_WORKING_DIR` | string | `./rag_storage` | LightRAG 工作目录 |

### 调度配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `MAX_CONCURRENT_ANALYSES` | int | `20` | 最大并发分析数 (1-100) |
| `OFFLINE_TRIGGER_MINUTES` | int | `5` | 离线去重 TTL (分钟) |

### 配额配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `DEFAULT_MONTHLY_TOKENS` | int | `40000000` | 新租户默认月度 Token 配额 |
| `QUOTA_WARNING_THRESHOLD` | float | `0.8` | 配额告警阈值 (0.0-1.0) |

---

## 数据库迁移

使用 Alembic 管理数据库 schema。

```bash
# 查看当前版本
alembic current

# 执行所有迁移
alembic upgrade head

# 回退一个版本
alembic downgrade -1

# 创建新迁移
alembic revision --autogenerate -m "描述"

# 查看 SQL (不执行)
alembic upgrade head --sql
```

### 迁移历史

| 版本 | 文件 | 内容 |
|------|------|------|
| 001 | `001_initial.py` | 创建 tenants, quotas, analysis_results 表 |
| 002 | `002_llm_providers.py` | 创建 llm_providers 表 |
| 003 | `003_llm_provider_type.py` | 添加 provider_type, max_tokens, timeout, extra_params 列 |

---

## 常用脚本

### 种子数据

```bash
# 创建测试租户 (admin_001, game_server_alpha, game_server_beta, disabled_tenant)
# 创建默认配额和分析结果样例
uv run python scripts/seed_data.py

# 从 .env 配置创建默认 LLM 提供商
uv run python scripts/seed_provider.py
```

### 测试脚本

```bash
# Agent 全流程测试 (需要完整基础设施 + .env)
# 测试 fetch_snapshot → RAG → 工具调用 → 分析 → 输出
uv run python scripts/test_agent_flow.py

# LLM 负载均衡测试
# 测试加权轮询、健康降级、缓存失效、回退机制
uv run python scripts/test_load_balancer.py

# LightRAG 集成测试 (需要 Milvus + Neo4j + Redis)
# 测试文档导入、多模式检索、Rerank
python -m scripts.tests.test_lightrag
```

### 单元测试

```bash
# 运行所有测试 (不需要真实基础设施，全部 mock)
pytest tests/ -v

# 运行单个测试文件
pytest tests/test_api.py -v

# 运行集成测试
pytest tests/integration/ -v
```

---

## 监控与健康检查

### 应用健康检查

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "2.0.0"}
```

### Docker 服务健康检查

```bash
# 查看所有服务状态
docker-compose -f docker-compose.dev.yml ps

# 查看某个服务日志
docker-compose -f docker-compose.dev.yml logs -f postgres
docker-compose -f docker-compose.dev.yml logs -f redis

# 检查 PostgreSQL
docker exec myagent_dev_postgres pg_isready -U myagent -d myagent

# 检查 Redis
docker exec myagent_dev_redis redis-cli -a myagent ping

# 检查 Neo4j
curl http://localhost:7474

# 检查 Milvus
curl http://localhost:9091/healthz
```

---

## 常见问题排查

### 服务启动失败: `Settings validation error`

`.env` 中缺少必填字段。检查 `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `EMBEDDING_API_KEY`, `RERANK_API_KEY`, `POSTGRES_DSN`, `NEO4J_PASSWORD` 是否已填写。

### 数据库连接失败

1. 确认 PostgreSQL 容器已启动: `docker ps | grep postgres`
2. 确认 `POSTGRES_DSN` 中的用户名、密码、端口与 `docker-compose.dev.yml` 一致
3. 默认: `postgresql+asyncpg://myagent:myagent@localhost:5432/myagent`

### Redis 连接失败

1. 确认 Redis 容器已启动
2. Docker Compose 的 Redis 有密码 (`myagent`)，`.env` 中应配置: `REDIS_URL=redis://:myagent@localhost:6379/0`

### Neo4j 连接失败

1. Neo4j 启动较慢（约 30 秒），等待 `healthy` 状态
2. 默认密码: `myagent123`

### Milvus 连接失败

1. Milvus 依赖 etcd 和 MinIO，等待两者 `healthy` 后 Milvus 才能启动
2. 总启动时间约 60 秒

### 分析结果为空 / 推荐质量差

1. 检查知识库是否有游戏文档 — 无文档时 RAG 返回空，LLM 只能做泛泛分析
2. 检查快照数据是否完整 — 缺少关键字段（pvp_rating, play_hours 等）会降低分析质量
3. 检查 LLM API 是否正常 — 查看日志中的错误信息

### 请求返回 429

请求频率超过 100 次/分钟。等待 60 秒后重试，或联系管理员调整限流参数。

### 停止所有服务

```bash
# 停止应用
Ctrl+C

# 停止基础设施（保留数据）
docker-compose -f docker-compose.dev.yml down

# 停止并清除数据
docker-compose -f docker-compose.dev.yml down -v
```
