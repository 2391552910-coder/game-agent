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


def _response(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "speakerRoleId": 10001,
        "targetRoleId": 10002,
        "pairKey": "10001:10002:event-123",
        "content": "我已经上线了，等你一起走。",
    }
    payload.update(overrides)
    return payload


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    base_url: str = "http://auto-chat.local/",
) -> AutoChatClient:
    return AutoChatClient(
        base_url=base_url,
        timeout_seconds=45,
        transport=transport,
    )


async def _generate(
    client: AutoChatClient,
    *,
    speaker_role_id: int = 10001,
    target_role_id: int = 10002,
    event_id: str = "event-123",
    question: str = "你最近参加了什么活动？",
):
    return await client.generate(
        speaker_role_id=speaker_role_id,
        target_role_id=target_role_id,
        event_id=event_id,
        question=question,
    )


async def test_client_sends_exact_four_field_request_without_auth() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response())

    result = await _generate(_client(httpx.MockTransport(handler)))

    assert result.content == "我已经上线了，等你一起走。"
    assert len(requests) == 1
    assert str(requests[0].url) == "http://auto-chat.local/chat/message"
    assert requests[0].headers["content-type"] == "application/json"
    assert requests[0].content.decode("utf-8") == (
        '{"speakerRoleId":10001,"targetRoleId":10002,'
        '"pairKey":"10001:10002:event-123",'
        '"question":"你最近参加了什么活动？"}'
    )
    assert {
        "authorization",
        "x-api-key",
        "x-appid",
        "x-signature",
        "x-timestampms",
        "x-requestid",
    }.isdisjoint(requests[0].headers)


async def test_client_accepts_full_message_endpoint_as_base_url() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_response())

    await _generate(
        _client(
            httpx.MockTransport(handler),
            base_url="http://auto-chat.local/chat/message",
        )
    )

    assert paths == ["/chat/message"]


@pytest.mark.parametrize(
    ("response_overrides", "category"),
    [
        ({"speakerRoleId": 99999}, "response_identity_mismatch"),
        ({"targetRoleId": 99999}, "response_identity_mismatch"),
        ({"pairKey": "10001:10002:other-event"}, "response_identity_mismatch"),
        ({"content": "   "}, "response_schema_invalid"),
        ({"content": "长" * 1001}, "response_schema_invalid"),
        ({"summaryVersion": 1}, "response_schema_invalid"),
    ],
)
async def test_client_rejects_invalid_auto_chat_response(
    response_overrides: dict[str, object],
    category: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response(**response_overrides))

    with pytest.raises(AutoChatPermanentError) as raised:
        await _generate(_client(httpx.MockTransport(handler)))

    assert raised.value.category == category


async def test_client_rejects_non_json_response_as_permanent_contract_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(AutoChatPermanentError) as raised:
        await _generate(_client(httpx.MockTransport(handler)))

    assert raised.value.category == "response_not_json"


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_client_classifies_server_failures_as_retryable(status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "unavailable"})

    with pytest.raises(AutoChatRetryableError) as raised:
        await _generate(_client(httpx.MockTransport(handler)))

    assert raised.value.category == "upstream_server_error"


@pytest.mark.parametrize("status_code", [400, 401, 409, 422])
async def test_client_classifies_client_failures_as_permanent(status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "invalid"})

    with pytest.raises(AutoChatPermanentError) as raised:
        await _generate(_client(httpx.MockTransport(handler)))

    assert raised.value.category == "upstream_request_rejected"


async def test_client_classifies_timeout_without_leaking_external_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret-token", request=request)

    with pytest.raises(AutoChatRetryableError) as raised:
        await _generate(_client(httpx.MockTransport(handler)))

    assert raised.value.category == "timeout"
    assert str(raised.value) == "auto chat request failed"


async def test_client_classifies_network_error_as_retryable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    with pytest.raises(AutoChatRetryableError) as raised:
        await _generate(_client(httpx.MockTransport(handler)))

    assert raised.value.category == "request_failed"


@pytest.mark.parametrize(
    "event_id",
    ["", "event:123", "事件-123", "event 123", "x" * 65],
)
async def test_client_rejects_invalid_event_id_before_http(event_id: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response())

    with pytest.raises(AutoChatPermanentError) as raised:
        await _generate(_client(httpx.MockTransport(handler)), event_id=event_id)

    assert raised.value.category == "request_schema_invalid"
    assert calls == 0


async def test_client_rejects_same_role_and_blank_question_before_http() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response())

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AutoChatPermanentError) as same_role:
        await _generate(client, target_role_id=10001)
    with pytest.raises(AutoChatPermanentError) as blank_question:
        await _generate(client, question="  ")

    assert same_role.value.category == "request_schema_invalid"
    assert blank_question.value.category == "request_schema_invalid"
    assert calls == 0


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
