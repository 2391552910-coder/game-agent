"""LLM 提供商工厂和注册表。

支持多种 LLM 提供商，统一返回 LangChain BaseChatModel 实例。
"""

import logging
from types import NoneType
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from src.core.llm.models import LLMProviderConfig

logger = logging.getLogger(__name__)

# 提供商注册表：provider_type -> LangChain Chat Model 类
PROVIDER_REGISTRY: dict[str, Any] = {
    "openai": ChatOpenAI,  # OpenAI 官方 API
    "deepseek": ChatOpenAI,  # DeepSeek（OpenAI 兼容）
    "qwen": ChatOpenAI,  # 通义千问（OpenAI 兼容）
    "zhipu": ChatOpenAI,  # 智谱 AI（OpenAI 兼容）
    "grok": ChatOpenAI,  # Grok（OpenAI 兼容）
}

# 延迟加载 Anthropic（需要额外依赖）
_anthropic_available = False
try:
    from langchain_anthropic import ChatAnthropic

    PROVIDER_REGISTRY["anthropic"] = ChatAnthropic
    _anthropic_available = True
    logger.debug("[providers] Anthropic provider 已启用")
except ImportError:
    logger.debug("[providers] langchain-anthropic 未安装，Anthropic provider 不可用")


def get_available_providers() -> list[str]:
    """获取所有可用的提供商类型。"""
    return list(PROVIDER_REGISTRY.keys())


def create_provider(config: LLMProviderConfig, temperature: float = 0.1) -> BaseChatModel:
    """根据配置创建对应的 LangChain Chat Model 实例。

    Args:
        config: LLM 提供商配置
        temperature: 生成温度

    Returns:
        LangChain BaseChatModel 实例

    Raises:
        ValueError: 未知的提供商类型
    """
    provider_type = config.provider_type or "openai"  # 默认使用 OpenAI 兼容

    model_class = PROVIDER_REGISTRY.get(provider_type)
    if model_class is None:
        available = ", ".join(get_available_providers())
        raise ValueError(
            f"未知的 provider_type: {provider_type}，"
            f"可用类型: {available}。"
            f"如需使用 Anthropic，请安装: uv add langchain-anthropic"
        )

    # 构建通用参数
    common_params = {
        "model": config.model,
        "temperature": temperature,
        "api_key": config.api_key,
        "base_url": config.base_url,
        "streaming": False,
        "max_retries": 2,
    }

    # 添加可选参数
    if config.max_tokens:
        common_params["max_tokens"] = config.max_tokens
    if config.timeout:
        common_params["timeout"] = config.timeout
    if config.extra_params:
        common_params.update(config.extra_params)

    # Anthropic 特殊处理
    if provider_type == "anthropic" and _anthropic_available:
        # Anthropic 默认 max_tokens 较高
        if "max_tokens" not in common_params:
            common_params["max_tokens"] = 4096

    logger.debug(
        "[providers] 创建 %s provider: model=%s, temperature=%s",
        provider_type,
        config.model,
        temperature,
    )

    return model_class(**common_params)


__all__ = [
    "get_available_providers",
    "create_provider",
    "PROVIDER_REGISTRY",
]
