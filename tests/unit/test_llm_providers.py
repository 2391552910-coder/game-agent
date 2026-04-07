"""LLM 多提供商支持测试。"""

from src.core.llm.providers import get_available_providers, create_provider
from src.core.llm.models import LLMProviderConfig


def test_get_available_providers():
    """测试获取可用提供商列表。"""
    providers = get_available_providers()
    assert isinstance(providers, list)
    assert "openai" in providers
    assert "deepseek" in providers
    assert "anthropic" in providers


def test_create_openai_provider():
    """测试创建 OpenAI 提供商。"""
    config = LLMProviderConfig(
        id="test-openai",
        name="Test OpenAI",
        provider="openai",
        model="gpt-4o",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        weight=1,
        provider_type="openai",
    )

    llm = create_provider(config, temperature=0.5)

    # 验证返回的是 LangChain Chat Model
    assert llm is not None
    assert hasattr(llm, "ainvoke")
    assert hasattr(llm, "bind_tools")
    assert hasattr(llm, "with_structured_output")


def test_create_anthropic_provider():
    """测试创建 Anthropic 提供商。"""
    config = LLMProviderConfig(
        id="test-anthropic",
        name="Test Anthropic",
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        api_key="test-key",
        base_url="https://api.anthropic.com",
        weight=1,
        provider_type="anthropic",
    )

    llm = create_provider(config, temperature=0.5)

    # 验证返回的是 LangChain Chat Model
    assert llm is not None
    assert hasattr(llm, "ainvoke")
    assert hasattr(llm, "bind_tools")
    assert hasattr(llm, "with_structured_output")


def test_create_deepseek_provider():
    """测试创建 DeepSeek 提供商（使用 OpenAI 兼容接口）。"""
    config = LLMProviderConfig(
        id="test-deepseek",
        name="Test DeepSeek",
        provider="deepseek",
        model="deepseek-chat",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        weight=1,
        provider_type="deepseek",
    )

    llm = create_provider(config, temperature=0.5)

    # 验证返回的是 LangChain Chat Model
    assert llm is not None
    assert hasattr(llm, "ainvoke")


def test_provider_with_custom_params():
    """测试带自定义参数的提供商创建。"""
    config = LLMProviderConfig(
        id="test-custom",
        name="Test Custom",
        provider="openai",
        model="gpt-4o",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        weight=1,
        provider_type="openai",
        max_tokens=1000,
        timeout=30,
        extra_params={"top_p": 0.9},
    )

    llm = create_provider(config, temperature=0.5)
    assert llm is not None


def test_invalid_provider_type():
    """测试无效的提供商类型。"""
    config = LLMProviderConfig(
        id="test-invalid",
        name="Test Invalid",
        provider="invalid",
        model="invalid-model",
        api_key="test-key",
        base_url="https://api.invalid.com",
        weight=1,
        provider_type="invalid_provider",
    )

    try:
        create_provider(config)
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "未知的 provider_type" in str(e)
        assert "invalid_provider" in str(e)
