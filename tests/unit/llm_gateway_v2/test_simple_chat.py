from __future__ import annotations

import asyncio

import pytest

from src.core.integration.llm_gateway_v2.simple_chat import (
    SimpleChatRetryableError,
    SimpleChatRoute,
    SimpleChatRouter,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _Model:
    def __init__(self, response: str | None = None, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.calls = 0

    async def ainvoke(self, messages: object) -> _Response:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        assert self.response is not None
        return _Response(self.response)


@pytest.mark.asyncio
async def test_router_classifies_the_three_gateway_examples() -> None:
    responses = {
        "你好": '{"route":"simple","content":"你好，很高兴见到你。"}',
        "你最近参加了什么活动？": '{"route":"complex","content":""}',
        "你的名字叫什么": '{"route":"complex","content":""}',
    }

    class Model:
        async def ainvoke(self, messages: list[object]) -> _Response:
            question = str(getattr(messages[-1], "content", ""))
            if question != "你好":
                system_prompt = str(getattr(messages[0], "content", ""))
                assert "角色姓名、身份、个人经历、近期活动" in system_prompt
            return _Response(responses[question])

    router = SimpleChatRouter(model_factory=lambda: Model(), timeout_seconds=3.0)

    assert (await router.route("你好")).route == "simple"
    assert (await router.route("你最近参加了什么活动？")).route == "complex"
    assert (await router.route("你的名字叫什么")).route == "complex"


@pytest.mark.asyncio
async def test_router_returns_simple_answer_from_one_deepseek_call() -> None:
    model = _Model('{"route":"simple","content":"可以，马上处理。"}')
    router = SimpleChatRouter(model_factory=lambda: model, timeout_seconds=3.0)

    result = await router.route("可以帮我确认一下吗？")

    assert result == SimpleChatRoute(route="simple", content="可以，马上处理。")
    assert model.calls == 1


@pytest.mark.asyncio
async def test_router_timeout_is_retryable_and_respects_three_second_budget() -> None:
    model = _Model('{"route":"complex","content":""}', delay=0.05)
    router = SimpleChatRouter(model_factory=lambda: model, timeout_seconds=0.01)

    with pytest.raises(SimpleChatRetryableError) as error_info:
        await router.route("请分析这件事")
    assert error_info.value.category == "timeout"
