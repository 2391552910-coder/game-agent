# myAgent v2.0

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

# 6. 启动服务
uv run python main.py
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

## 许可证

私有项目，未授权禁止使用。
