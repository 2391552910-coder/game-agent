"""LLM 基础类型定义。

提供统一的 LLM 接口类型定义，使用 LangChain BaseChatModel 作为统一抽象。
"""

from typing import Any
from pydantic import BaseModel

from langchain_core.language_models.chat_models import BaseChatModel

# 统一的 LLM 返回类型
LLMType = BaseChatModel


class LLMResponse(BaseModel):
    """统一的 LLM 响应格式（可选用于内部包装）。

    主要用于未来可能需要的响应标准化，
    当前直接使用 LangChain 的 AIMessage 响应。
    """

    content: str
    model: str
    usage: dict[str, int] | None = None
    raw_response: Any = None
