"""RobotGateway 回调客户端测试。"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from src.core.integration.robotgateway_callback import (
    RobotGatewayCallbackError,
    RobotGatewayCallbackSkipped,
    build_llm_gateway_decision_payload,
    build_robotgateway_callback_headers,
    build_robotgateway_callback_payload,
    send_llm_gateway_decision,
    send_robotgateway_analysis_callback,
)


def test_build_robotgateway_callback_payload_contains_analysis_result():
    snapshot = {
        "level": 28,
        "profession": "程序员",
        "current_area": "商业区",
    }
    output = {
        "player_profile": {"engagement_level": "high"},
        "recommended_actions": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "arguments": {},
                "reason": "先观察当前状态",
                "priority": "high",
            },
        ],
    }

    payload = build_robotgateway_callback_payload(
        tenant_id="tenant_001",
        user_id="player_001",
        snapshot=snapshot,
        output=output,
    )

    assert payload["event_type"] == "analysis.completed"
    assert payload["tenant_id"] == "tenant_001"
    assert payload["user_id"] == "player_001"
    assert payload["snapshot"] == snapshot
    assert payload["analysis"] == output
    assert isinstance(payload["timestamp"], str)
    parsed_timestamp = datetime.fromisoformat(payload["timestamp"])
    assert parsed_timestamp.tzinfo == UTC


def test_build_robotgateway_callback_headers():
    assert build_robotgateway_callback_headers(api_key=None) == {"Content-Type": "application/json"}
    assert build_robotgateway_callback_headers(api_key="secret") == {
        "Content-Type": "application/json",
        "X-Callback-API-Key": "secret",
    }


@pytest.mark.asyncio
async def test_send_robotgateway_analysis_callback_skips_when_url_missing():
    with pytest.raises(RobotGatewayCallbackSkipped):
        await send_robotgateway_analysis_callback(
            callback_url=None,
            api_key=None,
            timeout_seconds=10.0,
            tenant_id="tenant_001",
            user_id="player_001",
            snapshot={},
            output={},
        )


@pytest.mark.asyncio
async def test_send_robotgateway_analysis_callback_posts_payload():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)

    await send_robotgateway_analysis_callback(
        callback_url="http://robotgateway.local/callbacks/analysis",
        api_key="secret",
        timeout_seconds=10.0,
        tenant_id="tenant_001",
        user_id="player_001",
        snapshot={"level": 28},
        output={"recommended_actions": []},
        transport=transport,
    )

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://robotgateway.local/callbacks/analysis"
    assert request.headers["X-Callback-API-Key"] == "secret"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body["event_type"] == "analysis.completed"
    assert body["tenant_id"] == "tenant_001"
    assert body["user_id"] == "player_001"
    assert body["snapshot"] == {"level": 28}
    assert body["analysis"] == {"recommended_actions": []}


@pytest.mark.asyncio
async def test_send_robotgateway_analysis_callback_raises_on_non_2xx():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "failed"})

    transport = httpx.MockTransport(handler)

    with pytest.raises(RobotGatewayCallbackError):
        await send_robotgateway_analysis_callback(
            callback_url="http://robotgateway.local/callbacks/analysis",
            api_key=None,
            timeout_seconds=10.0,
            tenant_id="tenant_001",
            user_id="player_001",
            snapshot={},
            output={},
            transport=transport,
        )


def test_build_llm_gateway_decision_payload_maps_recommended_action_to_call_skill():
    payload = build_llm_gateway_decision_payload(
        decision_id="decision-001",
        decision_lease_id="lease-001",
        recommended_action={
            "skillName": "move_to",
            "schemaVersion": "v1",
            "arguments": {"target": {"x": 1, "y": 0, "z": 2}},
            "reason": "内部分析原因不进入 decision 协议",
            "priority": "high",
            "ttlMs": 30000,
            "goal_metric": "level",
        },
    )

    assert payload == {
        "contractVersion": "llm-gateway-http-v1",
        "decisionId": "decision-001",
        "decisionLeaseId": "lease-001",
        "action": "call_skill",
        "skillName": "move_to",
        "schemaVersion": "v1",
        "arguments": {"target": {"x": 1, "y": 0, "z": 2}},
    }


def test_build_llm_gateway_decision_payload_supports_wait():
    payload = build_llm_gateway_decision_payload(
        decision_id="decision-002",
        decision_lease_id="lease-002",
        recommended_action={"action": "wait", "arguments": {"waitMs": 3000}},
    )

    assert payload == {
        "contractVersion": "llm-gateway-http-v1",
        "decisionId": "decision-002",
        "decisionLeaseId": "lease-002",
        "action": "wait",
        "arguments": {"waitMs": 3000},
    }


@pytest.mark.parametrize(
    "recommended_action",
    [
        {"action": "wait", "arguments": {"waitMs": -1}},
        {"action": "wait", "arguments": {"waitMs": "3000"}},
        {"action": "wait", "arguments": {"waitMs": 3000.5}},
        {"action": "wait", "arguments": {"waitMs": None}},
        {"action": "wait", "arguments": {"waitMs": 3000, "extra": True}},
        {"action": "wait", "arguments": []},
    ],
)
def test_build_llm_gateway_decision_payload_rejects_invalid_wait_arguments(recommended_action):
    with pytest.raises(ValueError):
        build_llm_gateway_decision_payload(
            decision_id="decision-invalid-wait",
            decision_lease_id="lease-invalid-wait",
            recommended_action=recommended_action,
        )


def test_build_llm_gateway_decision_payload_supports_stop_hosting():
    payload = build_llm_gateway_decision_payload(
        decision_id="decision-003",
        decision_lease_id="lease-003",
        recommended_action={"action": "stop_hosting"},
    )

    assert payload == {
        "contractVersion": "llm-gateway-http-v1",
        "decisionId": "decision-003",
        "decisionLeaseId": "lease-003",
        "action": "stop_hosting",
    }


def test_build_llm_gateway_decision_payload_rejects_stop_hosting_arguments():
    with pytest.raises(ValueError):
        build_llm_gateway_decision_payload(
            decision_id="decision-invalid-stop",
            decision_lease_id="lease-invalid-stop",
            recommended_action={"action": "stop_hosting", "arguments": {}},
        )


@pytest.mark.parametrize(
    "recommended_action",
    [
        {"skillName": "", "schemaVersion": "v1", "arguments": {}},
        {"skillName": "observe_state", "schemaVersion": "", "arguments": {}},
        {"skillName": "observe_state", "schemaVersion": "v1", "arguments": []},
        {"skillName": "observe_state", "schemaVersion": "v1"},
    ],
)
def test_build_llm_gateway_decision_payload_rejects_invalid_call_skill_fields(recommended_action):
    with pytest.raises(ValueError):
        build_llm_gateway_decision_payload(
            decision_id="decision-invalid-skill",
            decision_lease_id="lease-invalid-skill",
            recommended_action=recommended_action,
        )


@pytest.mark.asyncio
async def test_send_llm_gateway_decision_posts_signed_request():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "accepted", "reason": "ok", "skillCallId": "skill-001"})

    transport = httpx.MockTransport(handler)

    response_payload = await send_llm_gateway_decision(
        decision_url="http://robotgateway.local/api/v1/hosting/llm/decision",
        app_id="llm-to-gateway",
        app_secret="secret-llm",
        timeout_seconds=10.0,
        decision_id="decision-001",
        decision_lease_id="lease-001",
        recommended_action={
            "skillName": "observe_state",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "只用于内部日志",
            "priority": "high",
        },
        request_id="req-llm-001",
        timestamp_ms="1719999999500",
        transport=transport,
    )

    assert response_payload == {"status": "accepted", "reason": "ok", "skillCallId": "skill-001"}
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://robotgateway.local/api/v1/hosting/llm/decision"
    assert request.headers["X-AppId"] == "llm-to-gateway"
    assert request.headers["X-TimestampMs"] == "1719999999500"
    assert request.headers["X-RequestId"] == "req-llm-001"
    assert len(request.headers["X-Signature"]) == 64
    body = json.loads(request.content)
    assert body == {
        "contractVersion": "llm-gateway-http-v1",
        "decisionId": "decision-001",
        "decisionLeaseId": "lease-001",
        "action": "call_skill",
        "skillName": "observe_state",
        "schemaVersion": "v1",
        "arguments": {},
    }


@pytest.mark.asyncio
async def test_send_llm_gateway_decision_rejects_malformed_gateway_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "accepted"})

    transport = httpx.MockTransport(handler)

    with pytest.raises(RobotGatewayCallbackError):
        await send_llm_gateway_decision(
            decision_url="http://robotgateway.local/api/v1/hosting/llm/decision",
            app_id="llm-to-gateway",
            app_secret="secret-llm",
            timeout_seconds=10.0,
            decision_id="decision-001",
            decision_lease_id="lease-001",
            recommended_action={"skillName": "observe_state", "schemaVersion": "v1", "arguments": {}},
            request_id="req-llm-001",
            timestamp_ms="1719999999500",
            transport=transport,
        )
