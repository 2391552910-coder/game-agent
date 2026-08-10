from __future__ import annotations

import httpx
import pytest

from src.core.integration.llm_gateway_v2.auto_chat import (
    AutoChatClient,
    AutoChatPermanentError,
    AutoChatRetryableError,
    ConversationContext,
)


def _conversation(**overrides: object) -> ConversationContext:
    payload: dict[str, object] = {
        "conversationId": "conv-10001-10002-1",
        "pairKey": "10001:10002",
        "speakerRoleId": 10001,
        "targetRoleId": 10002,
        "brainUsername": "conv-10001",
        "historyRounds": [
            {
                "askRoleId": 10001,
                "askContent": "你今天上线吗？",
                "answerRoleId": 10002,
                "answerContent": "已经上线了。",
            }
        ],
        "completedRounds": 1,
        "maxRounds": 6,
        "expiresAtMs": 1_060_000,
    }
    payload.update(overrides)
    return ConversationContext.model_validate(payload)


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    now_ms: int = 1_000_000,
) -> AutoChatClient:
    return AutoChatClient(
        base_url="http://auto-chat.local/",
        timeout_seconds=45,
        deadline_safety_seconds=10,
        transport=transport,
        now_ms=lambda: now_ms,
    )


async def test_client_sends_exact_game_side_request_without_user_prompt() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "speakerRoleId": 10001,
                "targetRoleId": 10002,
                "pairKey": "10001:10002",
                "content": "我已经上线了，等你一起走。",
                "summaryVersion": 3,
                "summaryUpdatedAt": None,
            },
        )

    result = await _client(httpx.MockTransport(handler)).generate(_conversation())

    assert result.content == "我已经上线了，等你一起走。"
    assert len(requests) == 1
    assert str(requests[0].url) == "http://auto-chat.local/chat/message"
    assert requests[0].headers["content-type"] == "application/json"
    assert requests[0].read().decode("utf-8")
    assert requests[0].content.decode("utf-8") == (
        '{"speakerRoleId":10001,"targetRoleId":10002,'
        '"brainUsername":"conv-10001","historyRounds":'
        '[{"askRoleId":10001,"askContent":"你今天上线吗？",'
        '"answerRoleId":10002,"answerContent":"已经上线了。"}],'
        '"forceRefreshSummary":false}'
    )
    assert b"userPrompt" not in requests[0].content


async def test_client_caps_timeout_to_conversation_deadline_minus_safety_margin() -> None:
    observed_timeout: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]
        observed_timeout.append(timeout["read"])
        return httpx.Response(
            200,
            json={
                "speakerRoleId": 10001,
                "targetRoleId": 10002,
                "pairKey": "10001:10002",
                "content": "好的。",
                "summaryVersion": 0,
                "summaryUpdatedAt": None,
            },
        )

    conversation = _conversation(expiresAtMs=1_025_000)

    await _client(httpx.MockTransport(handler)).generate(conversation)

    assert observed_timeout == [15.0]


async def test_client_rejects_request_when_deadline_has_no_safety_window() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(AutoChatPermanentError) as raised:
        await _client(httpx.MockTransport(handler)).generate(
            _conversation(expiresAtMs=1_010_000)
        )

    assert raised.value.category == "deadline_exhausted"
    assert calls == 0


@pytest.mark.parametrize(
    ("response_overrides", "category"),
    [
        ({"speakerRoleId": 99999}, "response_identity_mismatch"),
        ({"targetRoleId": 99999}, "response_identity_mismatch"),
        ({"pairKey": "10001:99999"}, "response_identity_mismatch"),
        ({"content": "   "}, "response_schema_invalid"),
        ({"content": "长" * 81}, "response_schema_invalid"),
    ],
)
async def test_client_rejects_invalid_auto_chat_response(
    response_overrides: dict[str, object],
    category: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = {
            "speakerRoleId": 10001,
            "targetRoleId": 10002,
            "pairKey": "10001:10002",
            "content": "好的。",
            "summaryVersion": 0,
            "summaryUpdatedAt": None,
        }
        payload.update(response_overrides)
        return httpx.Response(200, json=payload)

    with pytest.raises(AutoChatPermanentError) as raised:
        await _client(httpx.MockTransport(handler)).generate(_conversation())

    assert raised.value.category == category


@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_client_classifies_server_failures_as_retryable(status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "unavailable"})

    with pytest.raises(AutoChatRetryableError) as raised:
        await _client(httpx.MockTransport(handler)).generate(_conversation())

    assert raised.value.category == "upstream_server_error"


@pytest.mark.parametrize("status_code", [400, 401, 422])
async def test_client_classifies_client_failures_as_permanent(status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "invalid"})

    with pytest.raises(AutoChatPermanentError) as raised:
        await _client(httpx.MockTransport(handler)).generate(_conversation())

    assert raised.value.category == "upstream_request_rejected"


async def test_client_classifies_timeout_without_leaking_external_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret-token", request=request)

    with pytest.raises(AutoChatRetryableError) as raised:
        await _client(httpx.MockTransport(handler)).generate(_conversation())

    assert raised.value.category == "timeout"
    assert str(raised.value) == "auto chat request failed"


def test_conversation_context_rejects_more_than_five_history_rounds() -> None:
    rounds = [
        {
            "askRoleId": 10001,
            "askContent": f"ask-{index}",
            "answerRoleId": 10002,
            "answerContent": f"answer-{index}",
        }
        for index in range(6)
    ]

    with pytest.raises(ValueError):
        _conversation(historyRounds=rounds)
