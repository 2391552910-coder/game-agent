# myAgent v2.0

<<<<<<< ours
多租户游戏玩家行为分析与预测平台。游戏服务器通过 Webhook 发送玩家在线/离线事件，平台自动获取玩家数据，结合 RAG 知识库检索和 LLM 推理，生成结构化的行为分析和推荐行动。

## 核心能力

- **自动化分析** — 玩家离线时自动触发，无需人工干预
- **RAG 知识增强** — 灌入游戏文档后，推荐基于真实游戏规则，而非泛泛而谈
- **结构化输出** — 返回 JSON 格式的行为画像 + 优先级排序的推荐行动
- **多租户隔离** — 每个游戏独立租户，数据完全隔离，独立配额
- **多 LLM 提供商** — 支持加权轮询负载均衡和健康降级
- **分布式去重** — Redis 原子操作防止重复分析

## 架构概览

```
游戏服务器                     myAgent 平台
──────────                     ────────────
                               ┌──────────────────────────────────┐
                               │  FastAPI (API 层)                 │
  POST /webhooks/player-event  │  ├─ 认证 / 限流 / 路由            │
  ──────────────────────────►  │  └─ Webhook 接收事件              │
                               │          │                        │
                               │  Scheduler (调度层)               │
                               │  ├─ Redis 去重 + 防抖             │
                               │  └─ asyncio 后台任务              │
                               │          │                        │
                               │  LangGraph Agent (Agent 层)       │
                               │  ├─ RAG 上下文检索                │
                               │  ├─ 工具调用 (历史/相似/知识库)    │
                               │  ├─ 行为分析 (快速模型)            │
                               │  └─ 行动推理 (主力模型)            │
                               │          │                        │
                               │  LightRAG (引擎层)                │
                               │  ├─ 向量检索 (Milvus)             │
                               │  ├─ 知识图谱 (Neo4j)              │
                               │  └─ 缓存 (Redis)                  │
  GET /api/v1/analysis/{id}    │          │                        │
  ◄──────────────────────────  │  PostgreSQL (存储分析结果)         │
                               └──────────────────────────────────┘
```

## 快速开始

```bash
# 1. 复制环境配置
cp .env.example .env
# 编辑 .env，填入 LLM API Key 等必填项

# 2. 启动基础设施
docker-compose -f docker-compose.dev.yml up -d

# 3. 安装依赖
uv sync

# 4. 数据库迁移
alembic upgrade head

# 5. 初始化种子数据（可选）
uv run python scripts/seed_data.py
uv run python scripts/seed_provider.py

# 6. 启动 API 服务
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

服务启动后访问 `http://localhost:8000/docs` 查看交互式 API 文档。

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| API | FastAPI + Uvicorn | HTTP 接口，异步框架 |
| Agent | LangGraph | 有状态图编排，6 节点线性流程 |
| RAG | LightRAG | 混合检索（向量 + 图谱 + 关键词） |
| 向量库 | Milvus | 语义向量存储与检索 |
| 图数据库 | Neo4j | 知识图谱存储 |
| 缓存 | Redis | 认证缓存、去重、限流、健康追踪 |
| 数据库 | PostgreSQL | 租户、配额、分析结果 |
| LLM | DeepSeek / OpenAI / Anthropic | 行为分析与推理（可插拔多提供商） |
| 包管理 | uv | Python 依赖管理 |

## 文档索引

| 文档 | 受众 | 内容 |
|------|------|------|
| [平台总览](docs/overview.md) | 管理层 | 平台价值、业务流程、接入概要 |
| [技术架构](docs/architecture.md) | 开发者 | 四层架构、数据流、数据库、Agent 图 |
| [对接指南](docs/integration-guide.md) | 游戏团队 | Webhook 接口、快照数据格式、接入步骤 |
| [API 文档](docs/api-reference.md) | 开发者 | 全部端点的请求/响应/错误码 |
| [部署指南](docs/deployment.md) | 运维 | 环境要求、Docker、配置、脚本 |

## 项目结构

```
src/
├── api/                    # FastAPI 应用
│   ├── main.py             # 入口、中间件、路由注册
│   ├── middleware.py        # 认证 + 限流
│   └── routes/             # 各业务端点
├── config.py               # Pydantic Settings (.env)
├── core/
│   ├── agents/             # LangGraph Agent
│   │   ├── orchestrator.py # 图构建与编译
│   │   ├── nodes.py        # 6 个分析节点
│   │   ├── tools.py        # 工具集（历史/相似/RAG）
│   │   ├── prompts.py      # LLM 提示词
│   │   ├── models.py       # Pydantic 输出模型
│   │   └── state.py        # 图状态定义
│   ├── engine/             # LightRAG 引擎封装
│   ├── infrastructure/     # 数据库/Redis/Neo4j/韧性
│   ├── llm/                # LLM 多提供商 + 负载均衡
│   └── scheduler/          # 离线分析调度与去重
└── game_specific/          # 游戏数据接口（对接点）
    └── connector.py        # PlayerSnapshot 定义 + 接入规范
```

## 开发命令

```bash
# 运行所有测试
pytest tests/ -v

# 代码检查
ruff check src/
mypy src/

# 格式化
ruff format src/

# Agent 流程测试（需要完整基础设施 + .env）
uv run python scripts/test_agent_flow.py

# LightRAG 集成测试
python -m scripts.tests.test_lightrag

# 负载均衡器测试
uv run python scripts/test_load_balancer.py
```
=======
多租户游戏玩家行为分析与决策推荐平台。游戏侧通过 Webhook 推送玩家在线、离线和行为检查点事件；平台基于 Redis 去重、Prefect 调度、LangGraph Agent、LightRAG 检索和多 LLM Provider 负载均衡，异步生成结构化玩家画像与可执行推荐行动，并可回调 RobotGateway。

## 核心能力

- **离线自动分析**：玩家离线事件触发后台分析流程，Webhook 立即返回，不阻塞游戏服务器。
- **行为检查点记录**：在线期间可持续上报 `behavior_checkpoint`，沉淀会话行为事件。
- **RAG 知识增强**：通过 LightRAG 联合 Milvus、Neo4j、Redis 检索游戏规则与上下文。
- **动态决策系统**：在行为分析前增加意图推断、目标评估、玩家记忆更新等节点。
- **结构化行动输出**：输出玩家画像与基础可执行动作，动作类型受 Pydantic 模型约束。
- **行动追踪与监督**：支持目标追踪摘要、异常检测、放弃目标识别和追踪状态更新。
- **多租户隔离**：租户通过 API Key 鉴权，数据按 `tenant_id` 隔离，并支持 Token 配额。
- **LLM Provider 管理**：支持数据库配置多个 Provider，按模型类型、权重和健康状态调度。
- **RobotGateway 回调**：分析完成后可主动推送 `analysis.completed` 事件。

## 架构概览

```text
游戏服务器 / RobotGateway
        │
        │ POST /webhooks/player-event
        │  online / offline / behavior_checkpoint
        ▼
┌──────────────────────────────────────────────────────────────┐
│ FastAPI API 层                                                │
│  ├─ AuthMiddleware: X-API-Key → Redis 缓存 → PostgreSQL       │
│  ├─ RateLimitMiddleware: Redis ZSET 滑动窗口限流              │
│  ├─ Webhook: 玩家事件接收                                     │
│  ├─ Analysis: 分析结果查询                                    │
│  ├─ Tenants / Quota: 租户注册与配额查询                       │
│  └─ Providers: LLM Provider 管理，管理员接口                  │
└──────────────────────────────────────────────────────────────┘
        │
        │ offline 事件
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Scheduler 调度层                                              │
│  ├─ Redis SET NX: debounce:{user_id} 分布式去重               │
│  ├─ Prefect Deployment: analysis_flow/offline-analysis        │
│  └─ online 事件可删除去重 Key 并取消 Flow Run                 │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Prefect analysis_flow                                         │
│  ├─ fetch_snapshot_task: 使用 Webhook 快照或主动拉取          │
│  ├─ run_agent_task: 执行 LangGraph Agent                      │
│  ├─ store_result_task: 写入 analysis_results                  │
│  └─ send_callback_task: 回调 RobotGateway                     │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ LangGraph Agent                                               │
│  START                                                        │
│   → fetch_snapshot                                            │
│   → retrieve_rag_context                                      │
│   → intent_inference                                          │
│   → goal_evaluation                                           │
│   → gather_context                                            │
│   → behavior_analysis                                         │
│   → action_reasoning                                          │
│   → merge_output                                              │
│   → tracking_update                                           │
│   → memory_update                                             │
│   → END                                                       │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ 基础设施                                                      │
│  PostgreSQL: 租户、配额、分析结果、Provider、记忆与追踪数据   │
│  Redis: 认证缓存、限流、离线去重、LightRAG KV/缓存            │
│  Milvus: 向量检索                                             │
│  Neo4j: 知识图谱                                              │
│  Prefect: 异步 Flow 编排                                      │
└──────────────────────────────────────────────────────────────┘
```

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| API | FastAPI + Uvicorn | HTTP 接口、中间件、路由注册 |
| 调度 | Prefect 3 | 离线分析 Flow、后台任务执行、重试 |
| Agent | LangGraph | 状态图编排、动态决策与推荐生成 |
| RAG | LightRAG | 混合检索、知识增强上下文 |
| 向量库 | Milvus | 语义向量存储与检索 |
| 图数据库 | Neo4j | 知识图谱存储 |
| 缓存 | Redis | 鉴权缓存、限流、去重、LightRAG 缓存 |
| 数据库 | PostgreSQL | 业务数据、分析结果、Provider、记忆、追踪 |
| LLM | DeepSeek / OpenAI / Anthropic 等 | 行为分析、意图推断、行动推理 |
| 包管理 | uv | Python 依赖和命令运行 |
| 迁移 | Alembic | 数据库 Schema 管理 |

## 环境要求

| 组件 | 要求 |
|------|------|
| Python | `>=3.11,<3.13` |
| uv | 用于同步依赖和运行脚本 |
| Docker / Docker Compose | 用于开发环境基础设施 |
| LLM API Key | DeepSeek、OpenAI 或兼容 OpenAI API 的服务 |
| DashScope API Key | Embedding 与 Rerank 默认使用 Qwen 相关服务 |

## 快速开始

### 1. 准备环境变量

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少确认以下值与本地环境一致：

```env
OPENAI_API_KEY=sk-your-key
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_DEFAULT_MODEL=deepseek-chat
OPENAI_FAST_MODEL=deepseek-chat

EMBEDDING_API_KEY=sk-your-dashscope-key
RERANK_API_KEY=sk-your-dashscope-key

POSTGRES_DSN=postgresql+asyncpg://myagent:myagent@localhost:5432/myagent
REDIS_URL=redis://:myagent@localhost:6379/0
NEO4J_PASSWORD=myagent123
MILVUS_URI=http://localhost:19530

RAG_WORKING_DIR=./rag_storage
```

如果需要分析完成后回调 RobotGateway，额外配置：

```env
ROBOTGATEWAY_CALLBACK_URL=http://localhost:9000/callbacks/analysis
ROBOTGATEWAY_CALLBACK_API_KEY=your-callback-secret
ROBOTGATEWAY_CALLBACK_TIMEOUT_SECONDS=10
```

### 2. 启动基础设施

```powershell
docker-compose -f docker-compose.dev.yml up -d
docker-compose -f docker-compose.dev.yml ps
```

开发编排会启动 PostgreSQL、Redis、Neo4j、Milvus、etcd、MinIO 和 Prefect Server。首次启动 Milvus 与 Neo4j 需要等待健康检查完成。

### 3. 安装依赖

```powershell
uv sync
```

### 4. 执行数据库迁移

```powershell
uv run alembic upgrade head
```

### 5. 初始化开发数据

```powershell
uv run python scripts/seed_data.py
uv run python scripts/seed_provider.py
```

`seed_data.py` 会创建测试租户、配额和样例分析结果；`seed_provider.py` 会根据 `.env` 创建默认 LLM Provider。

常用测试 API Key：

| 租户 | API Key | 权限 |
|------|---------|------|
| `admin_001` | `gap_test_admin_key_001` | 管理员，可访问 Provider 管理接口 |
| `game_server_alpha` | `gap_test_alpha_key_002` | 普通租户 |
| `game_server_beta` | `gap_test_beta_key_003` | 普通租户 |

### 6. 注册 Prefect Deployment 并启动 Worker

离线分析通过 `analysis_flow/offline-analysis` 这个 Prefect Deployment 运行。首次使用前需要注册 Deployment，并启动 Worker。

```powershell
uv run python scripts/setup_prefect.py
uv run prefect worker start --pool default-agent-pool
```

Prefect UI 默认地址为 `http://localhost:4200`。

### 7. 启动 API 服务

API 服务入口是 `src.api.main:app`。

```powershell
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后可访问：

| 地址 | 说明 |
|------|------|
| `http://localhost:8000/health` | 健康检查 |
| `http://localhost:8000/docs` | Swagger API 文档 |
| `http://localhost:8000/redoc` | ReDoc API 文档 |

## 基础接口示例

### 健康检查

```powershell
curl http://localhost:8000/health
```

响应：

```json
{"status": "ok", "version": "2.0.0"}
```

### 注册租户

```powershell
curl -X POST http://localhost:8000/api/v1/tenants/register `
  -H "Content-Type: application/json" `
  -d "{\"user_id\":\"my_game_alpha\"}"
```

响应中会返回后续调用使用的 `api_key`。

### 上报离线事件

```powershell
curl -X POST http://localhost:8000/webhooks/player-event `
  -H "Content-Type: application/json" `
  -H "X-API-Key: gap_test_alpha_key_002" `
  -d "{
    \"user_id\":\"player_001\",
    \"event_type\":\"offline\",
    \"timestamp\":1779235200,
    \"snapshot\":{\"level\":42,\"pvp_rating\":1800}
  }"
```

可能响应：

```json
{"status": "scheduled", "user_id": "player_001", "flow_run_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
```

如果同一玩家在去重窗口内重复离线，响应：

```json
{"status": "debounced", "user_id": "player_001"}
```

### 上报上线事件

```powershell
curl -X POST http://localhost:8000/webhooks/player-event `
  -H "Content-Type: application/json" `
  -H "X-API-Key: gap_test_alpha_key_002" `
  -d "{\"user_id\":\"player_001\",\"event_type\":\"online\",\"timestamp\":1779235500}"
```

响应：

```json
{"status": "cancelled", "user_id": "player_001"}
```

### 上报行为检查点

```powershell
curl -X POST http://localhost:8000/webhooks/player-event `
  -H "Content-Type: application/json" `
  -H "X-API-Key: gap_test_alpha_key_002" `
  -d "{
    \"user_id\":\"player_001\",
    \"event_type\":\"behavior_checkpoint\",
    \"timestamp\":1779235600,
    \"session_id\":\"session_001\",
    \"behavior_event\":{\"type\":\"quest_completed\",\"data\":{\"quest_id\":\"main_03\"}},
    \"snapshot\":{\"level\":43}
  }"
```

响应：

```json
{"status": "recorded", "user_id": "player_001"}
```

### 查询最新分析结果

```powershell
curl http://localhost:8000/api/v1/analysis/player_001/latest `
  -H "X-API-Key: gap_test_alpha_key_002"
```

### 查询分析历史

```powershell
curl "http://localhost:8000/api/v1/analysis/player_001/history?limit=5" `
  -H "X-API-Key: gap_test_alpha_key_002"
```

### 查询配额

```powershell
curl http://localhost:8000/api/v1/quota/usage `
  -H "X-API-Key: gap_test_alpha_key_002"
```

### 管理 LLM Provider

Provider 接口需要管理员租户 API Key。

```powershell
curl http://localhost:8000/api/v1/providers `
  -H "X-API-Key: gap_test_admin_key_001"
```

## 配置说明

配置由 `src/config.py` 通过 `.env` 加载。字段名大小写不敏感。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ENV` | `development` | 运行环境 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `APP_WORKERS` | `1` | 应用工作进程数 |
| `CORS_ALLOWED_ORIGINS` | `["http://localhost:8000"]` | CORS 白名单 JSON 数组 |
| `LLM_PROVIDER` | `deepseek` | 默认 LLM Provider 名称 |
| `OPENAI_API_KEY` | 必填 | 兼容 OpenAI SDK 的 LLM API Key |
| `OPENAI_BASE_URL` | 必填 | 兼容 OpenAI SDK 的 Base URL |
| `OPENAI_DEFAULT_MODEL` | `deepseek-chat` | 主力模型，用于行动推理等任务 |
| `OPENAI_FAST_MODEL` | `deepseek-chat` | 快速模型，用于轻量推断任务 |
| `EMBEDDING_API_KEY` | 必填 | Embedding API Key |
| `EMBEDDING_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Embedding Base URL |
| `EMBEDDING_MODEL` | `text-embedding-v4` | Embedding 模型 |
| `EMBEDDING_DIM` | `1024` | 向量维度 |
| `RERANK_API_KEY` | 必填 | Rerank API Key |
| `RERANK_MODEL` | `gte-rerank-v2` | Rerank 模型 |
| `POSTGRES_DSN` | 必填 | PostgreSQL 异步连接串 |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt 地址 |
| `NEO4J_USERNAME` | `neo4j` | Neo4j 用户名 |
| `NEO4J_PASSWORD` | 必填 | Neo4j 密码 |
| `NEO4J_DATABASE` | `neo4j` | Neo4j 数据库名 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串 |
| `MILVUS_URI` | `http://localhost:19530` | Milvus 地址 |
| `MILVUS_USER` | `root` | Milvus 用户名 |
| `MILVUS_PASSWORD` | 空 | Milvus 密码 |
| `MILVUS_DB_NAME` | `lightrag` | Milvus 数据库名 |
| `GAME_DB_DSN` | 空 | 可选，主动拉取玩家快照时使用 |
| `RAG_DEFAULT_STRATEGY` | `hybrid` | LightRAG 默认检索策略 |
| `RAG_WORKING_DIR` | `./rag_storage` | LightRAG 工作目录 |
| `MAX_CONCURRENT_ANALYSES` | `20` | 最大并发分析数 |
| `OFFLINE_TRIGGER_MINUTES` | `5` | 离线去重 TTL，单位分钟 |
| `ROBOTGATEWAY_CALLBACK_URL` | 空 | 分析完成回调地址 |
| `ROBOTGATEWAY_CALLBACK_TIMEOUT_SECONDS` | `10.0` | 回调超时时间 |
| `ROBOTGATEWAY_CALLBACK_API_KEY` | 空 | 回调请求头 `X-Callback-API-Key` |
| `DEFAULT_MONTHLY_TOKENS` | `40000000` | 新租户默认月度 Token 配额 |
| `QUOTA_WARNING_THRESHOLD` | `0.8` | 配额告警阈值 |

## 项目结构

```text
.
├── alembic/                         # 数据库迁移
├── docs/                            # 项目设计、架构、对接和技术文档
├── game_docs/                       # 可灌入 RAG 的游戏资料样例
├── scripts/                         # 初始化、测试、调试脚本
├── src/
│   ├── api/
│   │   ├── main.py                  # FastAPI 应用入口
│   │   ├── middleware.py            # API Key 鉴权与限流
│   │   └── routes/                  # Webhook、分析、租户、配额、Provider 路由
│   ├── config.py                    # Pydantic Settings
│   ├── core/
│   │   ├── agents/                  # LangGraph Agent、节点、提示词、输出模型
│   │   ├── engine/                  # LightRAG 引擎封装
│   │   ├── infrastructure/          # PostgreSQL、Redis、Neo4j、结果存储、韧性
│   │   ├── integration/             # RobotGateway 回调客户端
│   │   ├── llm/                     # LLM Provider、工厂、负载均衡
│   │   ├── output/                  # 输出相关模块
│   │   └── scheduler/               # Redis 去重、Prefect Flow
│   └── game_specific/               # 游戏侧快照接口与适配点
├── tests/                           # API、单元、集成测试
├── docker-compose.dev.yml           # 开发基础设施
├── pyproject.toml                   # 项目依赖与工具配置
└── README.md
```

## 常用开发命令

```powershell
# 同步依赖
uv sync

# 数据库迁移
uv run alembic upgrade head

# 启动开发基础设施
docker-compose -f docker-compose.dev.yml up -d

# 启动 API
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# 注册 Prefect Deployment
uv run python scripts/setup_prefect.py

# 启动 Prefect Worker
uv run prefect worker start --pool default-agent-pool

# 初始化测试租户与样例数据
uv run python scripts/seed_data.py

# 初始化 LLM Provider
uv run python scripts/seed_provider.py

# 运行全部测试
uv run pytest tests/ -v

# 运行 API 测试
uv run pytest tests/api/ -v

# 运行单元测试
uv run pytest tests/unit/ -v

# 代码检查
uv run ruff check src tests

# 格式化
uv run ruff format src tests

# 类型检查
uv run mypy src
```

## 关键开发入口

| 文件 | 说明 |
|------|------|
| `src/api/main.py` | FastAPI 应用、生命周期、中间件、路由注册 |
| `src/api/routes/webhooks.py` | 玩家事件入口，处理 `online`、`offline`、`behavior_checkpoint` |
| `src/core/scheduler/triggers.py` | Redis 去重、Prefect Deployment 调度、Flow Run 取消 |
| `src/core/scheduler/flows/analysis_flow.py` | 离线分析 Flow，串联快照、Agent、存储、回调 |
| `src/core/agents/orchestrator.py` | LangGraph 主图构建 |
| `src/core/agents/nodes.py` | 快照、RAG、上下文收集、行为分析、行动推理等节点 |
| `src/core/agents/decision_nodes.py` | 意图推断、目标评估、玩家记忆更新 |
| `src/core/agents/models.py` | 最终输出与推荐行动模型 |
| `src/core/llm/balancer.py` | LLM Provider 加权轮询与健康降级 |
| `src/core/engine/lightrag_engine.py` | LightRAG 初始化与查询封装 |
| `src/game_specific/connector.py` | 游戏玩家快照适配接口 |

## 文档索引

| 文档 | 内容 |
|------|------|
| `docs/overview.md` | 平台总览、业务价值、流程说明 |
| `docs/architecture.md` | 技术架构、数据流、数据库设计 |
| `docs/integration-guide.md` | RobotGateway / 游戏服务器对接指南 |
| `docs/api-reference.md` | API 请求、响应、错误码 |
| `docs/deployment.md` | 部署、配置、运维说明 |
| `docs/技术栈/Agent执行流程.md` | Agent 从 Webhook 到 LangGraph 的执行链路 |
| `docs/技术栈/Redis使用场景汇总.md` | Redis 在鉴权、限流、去重和缓存中的使用 |

## 注意事项

- API 服务入口是 `src.api.main:app`；启动服务请使用 `uvicorn src.api.main:app`。
- `POST /webhooks/player-event` 的 `offline` 分析依赖 Prefect Deployment 和 Worker，未启动 Worker 时只能成功提交或会在调度阶段失败。
- 非公开接口都需要 `X-API-Key`；`/health`、`/docs`、`/openapi.json`、`/redoc` 是公开路径。
- Provider 管理接口需要管理员租户，即 `request.state.is_admin = true`。
- 开发环境 Redis 默认设置了密码，`.env` 中 `REDIS_URL` 需要使用 `redis://:myagent@localhost:6379/0`。
- 如果 Webhook 已传入 `snapshot`，Prefect Flow 会直接使用该快照；否则会调用 `src.game_specific.fetch_player_snapshot()` 主动获取。
>>>>>>> theirs

## 许可证

私有项目，未授权禁止使用。
