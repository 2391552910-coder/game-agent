from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from src.core.integration.llm_gateway_v2.decision_client import (
    DecisionClientProtocolError,
    DecisionClientTransportError,
    GatewayV2DecisionClient,
)


def _client(transport: httpx.AsyncBaseTransport) -> GatewayV2DecisionClient:
    return GatewayV2DecisionClient(
        decision_url="http://gateway.local/api/v2/hosting/llm/decision",
        app_id="myagent-decisions",
        app_secret=SecretStr("outbound-secret"),
        timeout_seconds=1.0,
        transport=transport,
        request_id_factory=lambda: "request-1",
        now_ms=lambda: 1_719_999_999_000,
    )


@pytest.mark.parametrize("action", ["call_skill", "stop_hosting"])
async def test_client_accepts_skill_actions_only_with_skill_call_id(action: str) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "accepted", "reason": "ok", "skillCallId": "call-1"})

    raw_body = b'{"decisionId":"decision-1"}'
    result = await _client(httpx.MockTransport(handler)).send(action=action, raw_body=raw_body)

    assert result.status == "accepted"
    assert result.http_status == 200
    assert result.skill_call_id == "call-1"
    assert requests[0].content == raw_body
    assert requests[0].headers["X-AppId"] == "myagent-decisions"
    assert requests[0].headers["X-RequestId"] == "request-1"
    assert requests[0].headers["X-TimestampMs"] == "1719999999000"
    assert len(requests[0].headers["X-Signature"]) == 64


@pytest.mark.parametrize("action", ["wait", "no_op"])
async def test_client_accepts_non_skill_actions_without_skill_call_id(action: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, json={"status": "accepted", "reason": "ok"})

    result = await _client(httpx.MockTransport(handler)).send(action=action, raw_body=b"{}")

    assert result.status == "accepted"
    assert result.skill_call_id is None


async def test_client_parses_non_2xx_unknown_rejected_reason_before_http_classification() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"status": "rejected", "reason": "gateway_policy_v27"})

    result = await _client(httpx.MockTransport(handler)).send(action="wait", raw_body=b"{}")

    assert result.status == "rejected"
    assert result.reason == "gateway_policy_v27"
    assert result.http_status == 409
    assert result.is_idempotency_conflict is False


async def test_client_preserves_idempotency_conflict_as_structured_rejection() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"status": "rejected", "reason": "idempotency_key_conflict"})

    result = await _client(httpx.MockTransport(handler)).send(action="call_skill", raw_body=b"{}")

    assert result.status == "rejected"
    assert result.is_idempotency_conflict is True


async def test_client_rejects_non_json_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"upstream unavailable")

    with pytest.raises(DecisionClientProtocolError) as raised:
        await _client(httpx.MockTransport(handler)).send(action="wait", raw_body=b"{}")

    assert raised.value.category == "response_not_json"
    assert raised.value.http_status == 502


@pytest.mark.parametrize("action", ["call_skill", "stop_hosting"])
async def test_client_rejects_accepted_skill_action_without_skill_call_id(action: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "accepted", "reason": "ok"})

    with pytest.raises(DecisionClientProtocolError) as raised:
        await _client(httpx.MockTransport(handler)).send(action=action, raw_body=b"{}")

    assert raised.value.category == "accepted_skill_call_id_missing"


@pytest.mark.parametrize("action", ["wait", "no_op"])
async def test_client_rejects_skill_call_id_for_non_skill_action(action: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "accepted", "reason": "ok", "skillCallId": "call-1"})

    with pytest.raises(DecisionClientProtocolError) as raised:
        await _client(httpx.MockTransport(handler)).send(action=action, raw_body=b"{}")

    assert raised.value.category == "accepted_skill_call_id_unexpected"


async def test_client_rejects_non_2xx_accepted_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "accepted", "reason": "ok"})

    with pytest.raises(DecisionClientProtocolError) as raised:
        await _client(httpx.MockTransport(handler)).send(action="wait", raw_body=b"{}")

    assert raised.value.category == "accepted_http_status_invalid"


async def test_client_rejects_response_with_extra_gateway_fields() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "status": "accepted",
                    "reason": "ok",
                    "traceId": "must-not-create-a-new-lease-source",
                }
            ).encode(),
        )

    with pytest.raises(DecisionClientProtocolError) as raised:
        await _client(httpx.MockTransport(handler)).send(action="wait", raw_body=b"{}")

    assert raised.value.category == "response_schema_invalid"


async def test_client_maps_timeout_without_exposing_external_exception_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("token=must-not-leak", request=request)

    with pytest.raises(DecisionClientTransportError) as raised:
        await _client(httpx.MockTransport(handler)).send(action="wait", raw_body=b"exact-body")

    assert str(raised.value) == "gateway v2 decision transport failed"
    assert raised.value.category == "timeout"
