"""LLM 客户端工厂。

  提供 ChatOpenAI 实例，支持 with_structured_output 用于结构化输出。
"""

from langchain_openai import ChatOpenAI

from src.config import settings

_llm_cache : dict[str, ChatOpenAI] = {}

def get_llm(model_type: str = "default", temprature: float = 0.1) -> ChatOpenAI:
    """获取LLM实例
    Args:
        model_type: "default"/"Fast"
        temprature: 温度
    """
    cache_key = f"{model_type}_{temprature}"
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    model = (settings.openai_default_model if model_type=="default" else settings.openai_fast_model)

    llm = ChatOpenAI(
        model = model,
        temperature=temprature,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        streaming=False,
        max_retries=2,
    )
    _llm_cache[cache_key] = llm
    return llm
