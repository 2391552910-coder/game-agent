# AGENTS.md

## 语言与协作
- 通常使用中文回复，保持客观、理性、简洁。
- 先读现有代码和文档，再判断项目状态；不要基于旧印象回答。
- 除非用户明确要求，不要随意新建说明文档或扩大修改范围。
- 工作区可能已有用户改动，禁止回滚、覆盖或整理无关改动。

## 项目定位
- 本项目是 `myAgent` / `myAgent2`，定位为 AI 游戏决策层。
- RobotGateway 负责账号托管、游戏服连接、session 管理、动作校验和最终执行。
- myAgent 负责行为分析、意图识别、目标评估、RAG 知识增强和决策输出。
- 当前决策结果输出 AiRobotGateway 可消费的 `skillName + arguments`。
- 不要再把新协议写成旧的 `action_type + payload` 输出。
- myAgent 不直接调用游戏服协议，不托管 session，也不承担 Gateway 控制面 HMAC 签名。

## 通信链路
- RobotGateway -> myAgent：`POST /webhooks/player-event`。
- myAgent -> RobotGateway：缺少快照时可通过 HTTP 拉取玩家 snapshot。
- myAgent -> RobotGateway：分析完成后回调 `analysis.completed`。
- Webhook 使用 `X-API-Key` 识别租户，认证中间件写入 `request.state.tenant_id`。
- Callback 由 `src/core/integration/robotgateway_callback.py` 发送。
- Callback URL 未配置时跳过回调，不视为分析失败。

## 主流程
```text
RobotGateway offline/behavior_checkpoint
  -> FastAPI /webhooks/player-event
  -> Redis 防抖/去重
  -> Prefect run_deployment("analysis_flow/offline-analysis")
  -> analysis_flow 获取 snapshot
  -> LangGraph Agent
  -> PostgreSQL analysis_results
  -> RobotGateway callback
```

## Prefect
- `analysis_flow/offline-analysis` 是 Prefect Deployment 名，不只是 Python 函数名。
- 本地联调 webhook 前，要先启动 Prefect Server，并注册或 serve 该 Deployment。
- `scripts/run_analysis_flow_serve.cmd` 会设置 RobotGateway 相关环境变量并 serve `offline-analysis`。
- 报 `Deployment not found` 时，优先检查 deployment 是否注册、serve 进程是否仍在运行。

## LangGraph Agent
- 主图在 `src/core/agents/orchestrator.py`。
- 当前线性节点：
  `fetch_snapshot -> retrieve_rag_context -> intent_inference -> goal_evaluation -> gather_context -> behavior_analysis -> action_reasoning -> merge_output -> tracking_update -> memory_update`
- State 定义在 `src/core/agents/state.py`。
- 常见 State 字段：`rag_context`、`enriched_context`、`tracking_summary`、`intent_result`、`goal_evaluation_result`。
- `create_orchestrator()` 支持 PostgresSaver checkpointer。
- 主 Flow 当前使用 `build_orchestrator().compile()`。

## 决策输出
- 模型在 `src/core/agents/models.py`。
- `RecommendedAction` 外部 JSON 字段：`skillName`、`schemaVersion`、`arguments`、`reason`、`priority`、`ttlMs`。
- 第一阶段允许 Gateway skill：`observe_state`、`move_to`、`stop_move`、`jump`、`play_action`。
- `move_to.arguments.target` 必须包含数字 `x/y/z`。
- `play_action.arguments.action` 必填。
- `goal_metric`、`goal_value`、`expected_hours` 仍用于行动追踪。
- Pydantic 外部协议字段用 alias 暴露，内部字段保持 snake_case。

## RAG 与知识库
- LightRAG 封装在 `src/core/engine/lightrag_engine.py`。
- 存储后端包括 Redis、Neo4j、Milvus、PostgreSQL。
- `retrieve_rag_context_node` 会从 snapshot 文本值构造查询并执行 hybrid 检索。
- `dynamic_rag_query` 默认可能被配置关闭，不要假设 gather_context 一定能动态查 RAG。
- 坐标、动作枚举、活动规则应进入知识库或快照。
- 找不到可靠坐标或动作枚举时，LLM 不应编造，应降级为 `observe_state`。

## 数据库与状态
- PostgreSQL 使用 SQLAlchemy 2.0 async，入口 `src/core/infrastructure/db.py`。
- 主要业务表：`tenants`、`quotas`、`analysis_results`、`session_events`、`action_tracking`、`player_intent`、`player_memory`。
- `session_events` 保存在线行为事件，用于意图识别。
- `player_intent` 保存目标和决策结果。
- `player_memory` 保存长期画像和目标历史。
- `action_tracking.action_type` 字段名沿用历史命名，当前写入 Gateway `skillName`。

## 常用命令
```powershell
uv sync
docker compose -f docker-compose.dev.yml up -d
uv run alembic upgrade head
uv run pytest tests/unit -q
uv run pytest tests/api -q
uv run ruff check src tests
```

## 本地 HTTP 联调
- API 服务默认监听 `127.0.0.1:8000`。
- 模拟 RobotGateway 通常监听 `127.0.0.1:9000`。
- Prefect API 通常是 `http://127.0.0.1:4200/api`。
- 启动 API：`scripts/run_api_robotgateway.cmd`。
- 启动 Flow serve：`scripts/run_analysis_flow_serve.cmd`。

## 测试策略
- 行为改动先写或更新测试，再改生产代码。
- 优先跑相关单测，再按风险扩大到 `tests/unit`、`tests/api` 或 e2e。
- e2e 依赖 Prefect deployment、基础设施和外部服务；失败时先判断环境前置条件。
- Ruff 全量可能暴露历史问题；至少对本次修改涉及的生产文件跑 ruff check。

## 编码边界
- Python 3.11/3.12，包管理使用 `uv`，异步优先。
- Ruff 配置 `line-length = 120`，规则含 `E/F/I/N/W/UP/B/SIM`。
- 文件已有中文注释时可继续使用中文；新增注释应少而准。
- 不要将密钥、Gateway AppSecret、API Key 或账号密码写入仓库。
- 不要把 RobotGateway 控制面 `/api/v1/hosting/skill` 执行逻辑误写进 myAgent。
- 不要把 HTTP 200 当成 Gateway skill 成功；真正结果要看 Gateway 返回体的 `status`。
- 不要开放聊天、交易、抽奖、领奖、战斗、排行榜或底层协议动作，除非产品边界明确要求。
