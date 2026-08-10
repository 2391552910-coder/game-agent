from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from math import isfinite
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SimpleChatRouteName = Literal["simple", "complex"]


class _SimpleChatModel(Protocol):
    async def ainvoke(self, messages: Sequence[object]) -> object: ...


class SimpleChatRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    route: SimpleChatRouteName
    content: str = Field(default="", max_length=1000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_simple_content(self) -> SimpleChatRoute:
        if self.route == "simple" and not self.content:
            raise ValueError("simple route requires non-empty content")
        return self

    @classmethod
    def validate_route_content(cls, value: object) -> SimpleChatRoute:
        route = cls.model_validate(value)
        if route.route == "complex" and route.content:
            return route.model_copy(update={"content": ""})
        return route


class SimpleChatError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("simple chat routing failed")


class SimpleChatRetryableError(SimpleChatError):
    pass


class SimpleChatPermanentError(SimpleChatError):
    pass


class SimpleChatRouter:
    _SYSTEM_PROMPT = (
        "你是游戏对话复杂度路由器。请判断玩家最新消息是否是简单问题，并在简单时直接生成一句简短回复。"
        "只允许返回 JSON，不要 Markdown，不要额外解释。格式必须是："
        '{"route":"simple","content":"简短回答"} 或 '
        '{"route":"complex","content":""}。'
        "simple 仅适用于不依赖角色设定的问候、确认、感谢和通用寒暄。"
        "凡是涉及角色姓名、身份、个人经历、近期活动、偏好、关系、记忆、世界观或 persona 的问题，"
        "即使一句话可以回答，也必须判定为 complex；"
        "需要多步骤推理、规划、比较、分析、解释原因、结合历史上下文或连续追问的问题也必须判定为 complex。"
    )

    def __init__(
        self,
        *,
        model_factory: Callable[[], _SimpleChatModel | Awaitable[_SimpleChatModel]],
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._model_factory = model_factory
        self._timeout_seconds = min(timeout_seconds, 3.0)

    async def route(self, text: str) -> SimpleChatRoute:
        normalized = text.strip()
        if not normalized:
            raise SimpleChatPermanentError("empty_text")
        try:
            response = await asyncio.wait_for(
                self._invoke_model(normalized),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            raise SimpleChatRetryableError("timeout") from None
        except SimpleChatError:
            raise
        except Exception as error:
            raise SimpleChatRetryableError("model_request_failed") from error

        try:
            route = SimpleChatRoute.validate_route_content(_parse_json_object(_response_content(response)))
        except Exception as error:
            raise SimpleChatPermanentError("response_schema_invalid") from error
        return route

    async def _invoke_model(self, text: str) -> object:
        model = self._model_factory()
        if inspect.isawaitable(model):
            model = await model
        return await model.ainvoke(
            [
                SystemMessage(content=self._SYSTEM_PROMPT),
                HumanMessage(content=text),
            ]
        )


def _response_content(response: object) -> str:
    content: Any = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    raise ValueError("model response content is not text")


def _parse_json_object(raw: str) -> dict[str, object]:
    if not raw:
        raise ValueError("model response is empty")
    candidate = raw
    if "```" in candidate:
        candidate = candidate.replace("```json", "").replace("```", "").strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response must be an object")
    return value


__all__ = [
    "SimpleChatPermanentError",
    "SimpleChatRetryableError",
    "SimpleChatRoute",
    "SimpleChatRouter",
]
