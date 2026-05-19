"""RobotGateway 回调客户端测试。"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from src.core.integration.robotgateway_callback import (
    RobotGatewayCallbackError,
    RobotGatewayCallbackSkipped,
    build_robotgateway_callback_headers,
    build_robotgateway_callback_payload,
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
            {"action_type": "complete_learning_course", "priority": "high"},
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
