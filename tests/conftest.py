"""全局测试 fixtures。

所有 mock 均不依赖真实基础设施（PostgreSQL/Redis/LLM）。
"""

import json
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Mock 核心基础设施
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_settings():
    """覆盖 settings，避免读取 .env 导致必填校验失败。"""
    mock = MagicMock()
    mock.postgres_dsn = "postgresql+asyncpg://test:test@localhost/test"
    mock.redis_url = "redis://localhost:6379/0"
    mock.log_level = "WARNING"
    mock.openai_api_key = "sk-test-key"
    mock.openai_base_url = "https://api.test.com/v1"
    mock.openai_default_model = "test-model-default"
    mock.openai_fast_model = "test-model-fast"
    mock.llm_provider = "test"
    mock.cors_allowed_origins = ["http://localhost:3000"]
    mock.admin_api_key = "admin-test-key"
    mock.default_monthly_tokens = 100000
    mock.offline_trigger_minutes = 5
    with patch("src.config.settings", mock):
        yield mock


@pytest.fixture()
def mock_redis():
    """Mock Redis 客户端。"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)

    # Pipeline mock: pipeline() 返回一个 pipeline 对象
    # pipeline 上的方法是同步调用的（链式），最后 await execute()
    pipe = MagicMock()
    pipe.zremrangebyscore = MagicMock(return_value=None)
    pipe.zadd = MagicMock(return_value=None)
    pipe.zcard = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.execute = AsyncMock(return_value=[0, 1, 1, True])

    redis.pipeline = MagicMock(return_value=pipe)
    return redis


@pytest.fixture(autouse=True)
def _mock_redis_module(mock_redis):
    """全局替换 get_redis。"""
    async def _get_redis():
        return mock_redis

    redis_locations = [
        "src.core.infrastructure.redis.get_redis",
        "src.api.middleware.get_redis",
        "src.core.llm.balancer.get_redis",
    ]

    active_patches = []
    for loc in redis_locations:
        try:
            p = patch(loc, _get_redis)
            p.start()
            active_patches.append(p)
        except AttributeError:
            pass
    yield
    for p in active_patches:
        p.stop()


@pytest.fixture()
def mock_session():
    """Mock SQLAlchemy 异步 session。"""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()

    # 支持 async with get_session() as session
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    return session, ctx


@pytest.fixture(autouse=True)
def _mock_db_session(mock_session):
    """全局替换 get_session。

    由于各模块通过 `from ... import get_session` 导入，
    需要在所有导入点都打 patch。对尚未导入的模块跳过。
    """
    session, ctx = mock_session
    _get_session_fn = lambda: ctx

    # 所有可能导入 get_session 的模块
    import_locations = [
        "src.core.infrastructure.db.get_session",
        "src.core.infrastructure.result_store.get_session",
        "src.api.routes.analysis.get_session",
        "src.api.routes.quota.get_session",
        "src.api.routes.tenants.get_session",
        "src.api.routes.providers.get_session",
        "src.api.middleware.get_session",
        "src.core.agents.tools.get_session",
    ]

    active_patches = []
    for loc in import_locations:
        try:
            p = patch(loc, _get_session_fn)
            p.start()
            active_patches.append(p)
        except AttributeError:
            pass  # 模块尚未导入，跳过
    yield
    for p in active_patches:
        p.stop()


# ---------------------------------------------------------------------------
# 测试客户端
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client():
    """FastAPI 测试客户端。"""
    from src.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_llm():
    """Mock ChatOpenAI 实例。"""
    llm = MagicMock()
    llm.model_name = "test-model"
    llm.openai_api_base = "https://api.test.com/v1"
    llm.ainvoke = AsyncMock(return_value=MagicMock(content="mock response"))
    llm.bind_tools = MagicMock(return_value=llm)
    llm.with_structured_output = MagicMock(return_value=llm)
    return llm


@pytest.fixture()
def mock_llm_with_balancer(mock_llm):
    """Mock balancer.select 返回 mock LLM。"""
    with patch("src.core.llm.balancer.balancer") as balancer:
        balancer.select = AsyncMock(return_value=mock_llm)
        balancer.initialize = AsyncMock()
        balancer.invalidate_cache = MagicMock()
        yield balancer, mock_llm


# ---------------------------------------------------------------------------
# 测试数据工厂
# ---------------------------------------------------------------------------


def make_tenant_row(
    tenant_id: str | None = None,
    is_active: bool = True,
    is_admin: bool = False,
) -> MagicMock:
    """创建模拟 tenant 行。"""
    row = MagicMock()
    row.id = uuid.UUID(tenant_id) if tenant_id else uuid.uuid4()
    row.is_active = is_active
    row.is_admin = is_admin
    row.api_key = f"gap_{uuid.uuid4().hex[:24]}"
    return row


def make_analysis_row(
    output_json: dict | None = None,
    analyzed_at: datetime | None = None,
    user_id: str = "user-001",
) -> MagicMock:
    """创建模拟 analysis_results 行。"""
    row = MagicMock()
    row.output_json = json.dumps(output_json or {
        "player_profile": {
            "playstyle": "competitive",
            "engagement_level": "high",
            "current_goal": ["level up"],
            "bottlenecks": ["time"],
        },
        "recommended_actions": [
            {"action_type": "quest", "priority": "high", "reason": "test", "payload": {}},
        ],
    })
    row.analyzed_at = analyzed_at or datetime.now(UTC)
    row.user_id = user_id
    return row


def make_quota_row(
    monthly_limit: int = 100000,
    used: int = 5000,
    period_start: date | None = None,
    period_end: date | None = None,
) -> MagicMock:
    """创建模拟 quota 行。"""
    row = MagicMock()
    row.monthly_limit = monthly_limit
    row.used = used
    today = date.today()
    row.period_start = period_start or today.replace(day=1)
    row.period_end = period_end or date(today.year, today.month + 1, 1) if today.month < 12 else date(today.year + 1, 1, 1)
    return row


def make_provider_row(
    provider_id: str | None = None,
    name: str = "test-provider",
    provider: str = "test",
    model: str = "test-model",
    weight: int = 1,
    is_active: bool = True,
    model_type: str = "default",
) -> MagicMock:
    """创建模拟 llm_providers 行。"""
    row = MagicMock()
    row.id = uuid.UUID(provider_id) if provider_id else uuid.uuid4()
    row.name = name
    row.provider = provider
    row.model = model
    row.api_key = "sk-test-key"
    row.base_url = "https://api.test.com/v1"
    row.weight = weight
    row.is_active = is_active
    row.model_type = model_type
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row
