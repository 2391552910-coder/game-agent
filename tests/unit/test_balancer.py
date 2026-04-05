"""LoadBalancer 单元测试。

不依赖真实 DB/Redis，使用 mock 验证核心逻辑。
"""

import time
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.llm.balancer import (
    NoProviderAvailable,
    _CACHE_TTL,
    _HEALTH_KEY_PREFIX,
    _UNHEALTHY_THRESHOLD,
    LoadBalancer,
)
from src.core.llm.models import LLMProviderConfig


def _make_provider(
    pid: str = "p-001",
    name: str = "Test",
    weight: int = 1,
    model_type: str = "default",
) -> LLMProviderConfig:
    return LLMProviderConfig(
        id=pid, name=name, provider="test", model="test-model",
        api_key="sk-test", base_url="http://test", weight=weight,
        model_type=model_type,
    )


class TestWeightedRoundRobin:
    def test_build_rr_sequence(self):
        lb = LoadBalancer()
        providers = [
            _make_provider("A", weight=3),
            _make_provider("B", weight=2),
            _make_provider("C", weight=1),
        ]
        lb._build_rr_sequence("default", providers)
        assert lb._rr_sequences["default"] == ["A", "A", "A", "B", "B", "C"]

    def test_single_provider(self):
        lb = LoadBalancer()
        providers = [_make_provider("A", weight=5)]
        lb._build_rr_sequence("default", providers)
        assert lb._rr_sequences["default"] == ["A"] * 5

    def test_weighted_select_distribution(self):
        lb = LoadBalancer()
        providers = [
            _make_provider("A", weight=3),
            _make_provider("B", weight=1),
        ]
        lb._build_rr_sequence("default", providers)

        counter = Counter()
        for _ in range(40):
            selected = lb._weighted_select("default", providers)
            counter[selected.id] += 1

        assert counter["A"] == 30
        assert counter["B"] == 10

    def test_counter_wraps_around(self):
        lb = LoadBalancer()
        providers = [_make_provider("A", weight=1)]
        lb._build_rr_sequence("default", providers)

        for _ in range(100):
            selected = lb._weighted_select("default", providers)
            assert selected.id == "A"

    def test_empty_sequence_fallback(self):
        lb = LoadBalancer()
        providers = [_make_provider("A")]
        # 不调用 _build_rr_sequence，序列为空
        selected = lb._weighted_select("default", providers)
        assert selected.id == "A"


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_by_default(self, mock_redis):
        lb = LoadBalancer()
        mock_redis.get = AsyncMock(return_value=None)
        assert await lb._check_health("p-001") is True

    @pytest.mark.asyncio
    async def test_unhealthy_after_threshold(self, mock_redis):
        lb = LoadBalancer()
        mock_redis.get = AsyncMock(return_value=str(_UNHEALTHY_THRESHOLD).encode())
        assert await lb._check_health("p-001") is False

    @pytest.mark.asyncio
    async def test_below_threshold_is_healthy(self, mock_redis):
        lb = LoadBalancer()
        mock_redis.get = AsyncMock(return_value=b"3")
        assert await lb._check_health("p-001") is True

    @pytest.mark.asyncio
    async def test_redis_failure_defaults_healthy(self):
        lb = LoadBalancer()
        with patch("src.core.llm.balancer.LoadBalancer._check_health", AsyncMock(side_effect=Exception("Redis down"))):
            # _check_health 本身捕获异常返回 True
            pass
        # 直接测试内部逻辑
        with patch("src.core.infrastructure.redis.get_redis", AsyncMock(side_effect=Exception("down"))):
            assert await lb._check_health("p-001") is True


class TestReportFailure:
    @pytest.mark.asyncio
    async def test_increments_counter(self, mock_redis):
        lb = LoadBalancer()
        await lb.report_failure("p-001")
        mock_redis.incr.assert_called_once()

    @pytest.mark.asyncio
    async def test_sets_expiry(self, mock_redis):
        lb = LoadBalancer()
        await lb.report_failure("p-001")
        mock_redis.expire.assert_called_once()


class TestReportSuccess:
    @pytest.mark.asyncio
    async def test_resets_counter(self, mock_redis):
        lb = LoadBalancer()
        await lb.report_success("p-001")
        mock_redis.delete.assert_called_once_with(f"{_HEALTH_KEY_PREFIX}p-001")


class TestCacheInvalidation:
    def test_clears_all_caches(self):
        lb = LoadBalancer()
        lb._pool_cache["default"] = [_make_provider()]
        lb._rr_sequences["default"] = ["p-001"]
        lb._rr_counters["default"] = 5
        lb._pool_loaded_at["default"] = time.monotonic()

        lb.invalidate_cache()

        assert lb._pool_cache == {}
        assert lb._rr_sequences == {}
        assert lb._rr_counters == {}
        assert lb._pool_loaded_at == {}

    def test_does_not_clear_llm_cache(self):
        """invalidate_cache 只清 provider 缓存，不清 LLM 实例缓存。"""
        lb = LoadBalancer()
        lb._llm_cache["key"] = MagicMock()
        lb.invalidate_cache()
        assert "key" in lb._llm_cache


class TestSelect:
    @pytest.mark.asyncio
    async def test_no_provider_raises(self):
        lb = LoadBalancer()
        lb._pool_cache["default"] = []
        lb._pool_loaded_at["default"] = time.monotonic() + 9999

        # 需要绕过 _load_providers
        with patch.object(lb, "_load_providers", AsyncMock(return_value=[])):
            with pytest.raises(NoProviderAvailable):
                await lb.select("default")

    @pytest.mark.asyncio
    async def test_creates_llm_instance(self, mock_redis):
        lb = LoadBalancer()
        providers = [_make_provider()]
        lb._pool_cache["default"] = providers
        lb._pool_loaded_at["default"] = time.monotonic() + 9999
        lb._build_rr_sequence("default", providers)

        mock_redis.get = AsyncMock(return_value=None)

        with patch.object(lb, "_load_providers", AsyncMock(return_value=providers)):
            llm = await lb.select("default", temperature=0.5)

        assert llm is not None
        assert llm.model_name == "test-model"

    @pytest.mark.asyncio
    async def test_llm_instance_caching(self, mock_redis):
        """相同 provider + temperature 返回同一实例。"""
        lb = LoadBalancer()
        providers = [_make_provider()]
        lb._pool_cache["default"] = providers
        lb._pool_loaded_at["default"] = time.monotonic() + 9999
        lb._build_rr_sequence("default", providers)

        mock_redis.get = AsyncMock(return_value=None)

        with patch.object(lb, "_load_providers", AsyncMock(return_value=providers)):
            llm1 = await lb.select("default", temperature=0.1)
            llm2 = await lb.select("default", temperature=0.1)

        assert llm1 is llm2
