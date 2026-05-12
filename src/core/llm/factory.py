"""LLM 客户端工厂。

优先从 LoadBalancer 获取（DB 配置的 provider pool），
无可用 provider 时回退到 .env 配置。

支持多种 LLM 提供商，统一返回 LangChain BaseChatModel 实例。
"""

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from src.config import settings
from src.core.llm.models import LLMProviderConfig

logger = logging.getLogger(__name__)

# 回退缓存（.env 配置）
_fallback_cache: dict[str, BaseChatModel] = {}


async def get_llm(model_type: str = "default", temperature: float = 0.1) -> BaseChatModel:
    """获取 LLM 实例。

    优先从 LoadBalancer 获取（DB 配置的 provider pool），
    无可用 provider 时回退到 .env 配置。

    Args:
        model_type: "default" 或 "fast"
        temperature: 生成温度

    Returns:
        LangChain BaseChatModel 实例
    """
    from src.core.llm.balancer import NoProviderAvailable, balancer

    try:
        return await balancer.select(model_type, temperature)
    except NoProviderAvailable:
        logger.debug("[factory] 无 DB provider，回退到 .env 配置, model_type=%s", model_type)
        return _fallback_llm(model_type, temperature)


def _fallback_llm(model_type: str, temperature: float) -> BaseChatModel:
    """回退到 settings 配置（原逻辑保留）。"""
    cache_key = f"{model_type}_{temperature}"
    if cache_key in _fallback_cache:
        return _fallback_cache[cache_key]

    model = settings.openai_default_model if model_type == "default" else settings.openai_fast_model

    llm = ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        streaming=True,   # 兼容强制返回 SSE 的代理服务器
        max_retries=2,
    )
    _fallback_cache[cache_key] = llm
    return llm


def create_llm_from_config(config: LLMProviderConfig, temperature: float = 0.1) -> BaseChatModel:
    """根据配置创建对应的 LLM 实例。

    Args:
        config: LLM 提供商配置
        temperature: 生成温度

    Returns:
        LangChain BaseChatModel 实例
    """
    from src.core.llm.providers import create_provider

    return create_provider(config, temperature)
