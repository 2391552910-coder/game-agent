"""LLM 工厂测试。

factory.py 内部使用延迟导入 `from src.core.llm.balancer import balancer`，
因此 patch 目标是 `src.core.llm.balancer.balancer`。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.llm.balancer import NoProviderAvailable
from src.core.llm.factory import _fallback_llm, get_env_llm, get_llm

# patch factory 内部延迟导入的 balancer 单例
_BALANCER_PATH = "src.core.llm.balancer.balancer"
# factory 内部 from src.config import settings (顶层导入)
_FACTORY_SETTINGS = "src.core.llm.factory.settings"


class TestGetLlm:
    @pytest.mark.asyncio
    async def test_env_source_uses_settings_without_balancer(self):
        import src.core.llm.factory as factory_mod
        factory_mod._fallback_cache.clear()

        with patch(_BALANCER_PATH) as mock_balancer:
            mock_balancer.select = AsyncMock()
            with patch(_FACTORY_SETTINGS) as mock_s:
                mock_s.llm_provider_source = "env"
                mock_s.openai_default_model = "test-env-model"
                mock_s.openai_api_key = "sk-test"
                mock_s.openai_base_url = "http://test"
                result = await get_llm("default", 0.1)

        mock_balancer.select.assert_not_called()
        assert result.model_name == "test-env-model"

    @pytest.mark.asyncio
    async def test_db_source_balancer_success(self):
        mock_llm = MagicMock()
        mock_llm.model_name = "from-balancer"

        with patch(_BALANCER_PATH) as mock_balancer:
            mock_balancer.select = AsyncMock(return_value=mock_llm)
            with patch(_FACTORY_SETTINGS) as mock_s:
                mock_s.llm_provider_source = "db"
                result = await get_llm("default", 0.1)

        assert result.model_name == "from-balancer"

    @pytest.mark.asyncio
    async def test_db_source_fallback_on_no_provider(self):
        import src.core.llm.factory as factory_mod
        factory_mod._fallback_cache.clear()

        with patch(_BALANCER_PATH) as mock_balancer:
            mock_balancer.select = AsyncMock(side_effect=NoProviderAvailable())
            with patch(_FACTORY_SETTINGS) as mock_s:
                mock_s.llm_provider_source = "db"
                mock_s.openai_default_model = "test-model-default"
                mock_s.openai_api_key = "sk-test"
                mock_s.openai_base_url = "http://test"
                result = await get_llm("default", 0.1)

        assert result.model_name == "test-model-default"

    @pytest.mark.asyncio
    async def test_fast_model_type(self):
        import src.core.llm.factory as factory_mod
        factory_mod._fallback_cache.clear()

        with patch(_BALANCER_PATH) as mock_balancer:
            mock_balancer.select = AsyncMock(side_effect=NoProviderAvailable())
            with patch(_FACTORY_SETTINGS) as mock_s:
                mock_s.llm_provider_source = "db"
                mock_s.openai_fast_model = "test-model-fast"
                mock_s.openai_api_key = "sk-test"
                mock_s.openai_base_url = "http://test"
                result = await get_llm("fast", 0.1)

        assert result.model_name == "test-model-fast"

    @pytest.mark.asyncio
    async def test_env_llm_always_uses_environment_fast_model(self):
        import src.core.llm.factory as factory_mod
        factory_mod._fallback_cache.clear()

        with patch(_FACTORY_SETTINGS) as mock_s:
            mock_s.openai_fast_model = "test-env-fast"
            mock_s.openai_api_key = "sk-test"
            mock_s.openai_base_url = "http://test"
            result = await get_env_llm("fast", 0.1, timeout_seconds=3, max_retries=0)

        assert result.model_name == "test-env-fast"


class TestFallbackLlm:
    def test_creates_instance(self):
        with patch(_FACTORY_SETTINGS) as mock_s:
            mock_s.openai_default_model = "test-model-default"
            mock_s.openai_api_key = "sk-test"
            mock_s.openai_base_url = "http://test"
            llm = _fallback_llm("default", 0.5)
        assert llm.model_name == "test-model-default"

    def test_caches_instance(self):
        import src.core.llm.factory as factory_mod
        factory_mod._fallback_cache.clear()

        with patch(_FACTORY_SETTINGS) as mock_s:
            mock_s.openai_default_model = "test-model-default"
            mock_s.openai_api_key = "sk-test"
            mock_s.openai_base_url = "http://test"
            llm1 = _fallback_llm("default", 0.1)
            llm2 = _fallback_llm("default", 0.1)
        assert llm1 is llm2

    def test_different_params_different_instance(self):
        import src.core.llm.factory as factory_mod
        factory_mod._fallback_cache.clear()

        with patch(_FACTORY_SETTINGS) as mock_s:
            mock_s.openai_default_model = "test-model-default"
            mock_s.openai_api_key = "sk-test"
            mock_s.openai_base_url = "http://test"
            llm1 = _fallback_llm("default", 0.1)
            llm2 = _fallback_llm("default", 0.5)
        assert llm1 is not llm2
