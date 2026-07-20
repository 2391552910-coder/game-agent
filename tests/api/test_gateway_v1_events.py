"""LLM Gateway v1 事件入口测试。"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

APP_ID = "gateway-to-llm"
APP_SECRET = "secret-gateway"


def _body_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _signed_headers(path: str, body: bytes, request_id: str = "req-001") -> dict[str, str]:
    timestamp_ms = "1719999999000"
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join(["POST", path, timestamp_ms, request_id, body_hash])
    signature = hmac.new(APP_SECRET.encode(), signing_text.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-AppId": APP_ID,
        "X-TimestampMs": timestamp_ms,
        "X-RequestId": request_id,
        "X-Signature": signature,
    }


def _session_started_payload(event_id: str = "evt-001") -> dict:
    return {
        "traceId": "trace-001",
        "gatewayId": "gateway-01",
        "contractVersion": "llm-gateway-http-v1",
        "event": {
            "eventId": event_id,
            "eventType": "session_started",
            "decisionLeaseId": "lease-001",
            "occurredAtMs": 1719999999000,
            "payload": {
                "session": {
                    "sessionId": "session-001",
                    "accountId": "account-001",
                    "roleId": "role-001",
                    "sceneId": 1001,
                    "state": "Running",
                    "position": {"x": 12.3, "y": 0, "z": 45.6},
                    "controllable": True,
                }
            },
        },
    }


def _batch_event(event_id: str = "evt-001") -> dict:
    return _session_started_payload(event_id=event_id)["event"]


def _event_batch_payload(*events: dict, trace_id: str = "trace-001") -> dict:
    return {
        "traceId": trace_id,
        "gatewayId": "gateway-01",
        "contractVersion": "llm-gateway-http-v1",
        "sentAtMs": 1719999999001,
        "events": list(events),
    }


def _configure_gateway_settings(settings):
    settings.llm_gateway_app_secrets = {APP_ID: APP_SECRET}
    settings.llm_gateway_timestamp_tolerance_ms = 10**12


def _set_nested(payload: dict, path: tuple[str, ...], value) -> None:
    current = payload
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


async def _post_gateway_payload(client, payload: dict, request_id: str = "req-001"):
    body = _body_bytes(payload)
    return await client.post(
        "/api/gateway/events",
        content=body,
        headers=_signed_headers("/api/gateway/events", body, request_id=request_id),
    )


def _assert_bad_request(response) -> None:
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "bad_request", "message": "bad request"}}


@pytest.mark.asyncio
async def test_gateway_v1_event_accepts_signed_session_started(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    body = _body_bytes(payload)
    response = await client.post(
        "/api/gateway/events",
        content=body,
        headers=_signed_headers("/api/gateway/events", body),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "eventId": "evt-001"}


@pytest.mark.asyncio
async def test_gateway_v1_event_batch_with_one_event_uses_single_event_flow(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _event_batch_payload(_batch_event())
    response = await _post_gateway_payload(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "eventId": "evt-001"}


@pytest.mark.asyncio
async def test_gateway_v1_event_batch_accepts_sgai_event_metadata(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    event = _batch_event()
    event["sessionId"] = "session-001"
    event["stateVersion"] = 1
    payload = _event_batch_payload(event)
    response = await _post_gateway_payload(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "eventId": "evt-001"}


@pytest.mark.asyncio
async def test_gateway_v1_event_batch_with_multiple_events_returns_batch_result(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _event_batch_payload(_batch_event("evt-001"), _batch_event("evt-002"))
    response = await _post_gateway_payload(client, payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "traceId": "trace-001",
        "results": [
            {"status": "accepted", "eventId": "evt-001"},
            {"status": "accepted", "eventId": "evt-002"},
        ],
    }


@pytest.mark.asyncio
async def test_gateway_v1_event_batch_duplicate_returns_batch_duplicate_result(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    first_payload = _event_batch_payload(_batch_event("evt-001"))
    first = await _post_gateway_payload(client, first_payload)
    second_payload = _event_batch_payload(_batch_event("evt-001"), _batch_event("evt-002"))
    second = await _post_gateway_payload(client, second_payload, request_id="req-002")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {
        "status": "accepted",
        "traceId": "trace-001",
        "results": [
            {"status": "duplicate", "eventId": "evt-001"},
            {"status": "accepted", "eventId": "evt-002"},
        ],
    }


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_query_parameters(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    body = _body_bytes(payload)
    response = await client.post(
        "/api/gateway/events?debug=1",
        content=body,
        headers=_signed_headers("/api/gateway/events", body),
    )

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_plain_text_body(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    body = b"not json"
    headers = _signed_headers("/api/gateway/events", body)
    headers["Content-Type"] = "text/plain"

    response = await client.post("/api/gateway/events", content=body, headers=headers)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_unknown_top_level_field(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    payload["extra"] = "unexpected"

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_missing_decision_lease_for_decision_event(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    del payload["event"]["decisionLeaseId"]

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_session_stopped_with_decision_lease(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    payload["event"]["eventType"] = "session_stopped"
    payload["event"]["payload"]["session"]["state"] = "Stopped"
    payload["event"]["payload"]["session"]["controllable"] = False
    payload["event"]["payload"]["stop"] = {"reason": "admin_stop"}

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_uncontrollable_session_started(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    payload["event"]["payload"]["session"]["controllable"] = False

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("event", "occurredAtMs"), True),
        (("event", "occurredAtMs"), 1719999999000.0),
        (("event", "occurredAtMs"), "1719999999000"),
        (("event", "payload", "session", "sceneId"), True),
        (("event", "payload", "session", "sceneId"), 1001.0),
        (("event", "payload", "session", "sceneId"), "1001"),
        (("event", "payload", "session", "controllable"), "true"),
        (("event", "payload", "session", "position", "x"), True),
        (("event", "payload", "session", "position", "x"), "12.3"),
    ],
)
@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_type_coercion_attacks(client, _mock_settings, path, value):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    _set_nested(payload, path, value)

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_non_finite_position_number(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    payload["event"]["payload"]["session"]["position"]["x"] = float("inf")

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_explicit_null_optional_block(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    payload["event"]["payload"]["skill"] = None

    response = await _post_gateway_payload(client, payload)

    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_v1_event_duplicate_returns_duplicate(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    body = _body_bytes(payload)
    headers = _signed_headers("/api/gateway/events", body)

    first = await client.post("/api/gateway/events", content=body, headers=headers)
    second = await client.post("/api/gateway/events", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "eventId": "evt-001"}


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_same_event_id_with_different_body(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    first_payload = _session_started_payload()
    first_body = _body_bytes(first_payload)
    await client.post(
        "/api/gateway/events",
        content=first_body,
        headers=_signed_headers("/api/gateway/events", first_body),
    )

    second_payload = _session_started_payload()
    second_payload["event"]["payload"]["session"]["position"]["x"] = 99
    second_body = _body_bytes(second_payload)
    response = await client.post(
        "/api/gateway/events",
        content=second_body,
        headers=_signed_headers("/api/gateway/events", second_body, request_id="req-002"),
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "bad_request", "message": "bad request"}}


@pytest.mark.asyncio
async def test_gateway_v1_idempotency_treats_failed_nx_set_as_duplicate(mock_redis, _mock_settings):
    from src.api.routes.webhooks import _claim_gateway_event_idempotency

    _mock_settings.llm_gateway_idempotency_ttl_seconds = 86_400
    body_sha = "a" * 64
    mock_redis.get = AsyncMock(side_effect=[None, body_sha])
    mock_redis.set = AsyncMock(return_value=False)

    status = await _claim_gateway_event_idempotency("evt-race-001", body_sha)

    assert status == "duplicate"
    mock_redis.set.assert_awaited_once_with("llm-gateway:event:evt-race-001", body_sha, ex=86_400, nx=True)


@pytest.mark.asyncio
async def test_gateway_v1_idempotency_treats_failed_nx_set_with_different_body_as_conflict(mock_redis, _mock_settings):
    from src.api.routes.webhooks import _claim_gateway_event_idempotency

    _mock_settings.llm_gateway_idempotency_ttl_seconds = 86_400
    mock_redis.get = AsyncMock(side_effect=[None, "b" * 64])
    mock_redis.set = AsyncMock(return_value=False)

    status = await _claim_gateway_event_idempotency("evt-race-002", "a" * 64)

    assert status == "conflict"


@pytest.mark.asyncio
async def test_gateway_v1_event_rejects_invalid_signature(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)

    payload = _session_started_payload()
    body = _body_bytes(payload)
    headers = _signed_headers("/api/gateway/events", body)
    headers["X-Signature"] = "0" * 64

    response = await client.post("/api/gateway/events", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "signature_invalid",
            "message": "request signature invalid",
        }
    }


@pytest.mark.asyncio
async def test_gateway_v1_event_with_lease_runs_agent_and_posts_decision(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    _mock_settings.llm_gateway_decision_url = "http://robotgateway.local/api/v1/hosting/llm/decision"
    _mock_settings.llm_gateway_decision_app_id = "llm-to-gateway"
    _mock_settings.llm_gateway_decision_app_secret = "secret-llm"
    _mock_settings.llm_gateway_decision_timeout_seconds = 10.0

    payload = _session_started_payload(event_id="evt-agent-001")
    payload["event"]["sessionId"] = "session-001"
    payload["event"]["stateVersion"] = 1
    body = _body_bytes(payload)
    agent_output = {
        "recommended_actions": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "arguments": {},
                "reason": "先观察",
                "priority": "high",
                "ttlMs": 30000,
            }
        ]
    }

    with (
        patch("src.api.routes.webhooks.run_gateway_v1_agent", AsyncMock(return_value=agent_output)) as mock_agent,
        patch(
            "src.api.routes.webhooks.send_llm_gateway_decision",
            AsyncMock(return_value={"status": "accepted"}),
        ) as mock_decision,
    ):
        response = await client.post(
            "/api/gateway/events",
            content=body,
            headers=_signed_headers("/api/gateway/events", body),
        )

    assert response.status_code == 200
    mock_agent.assert_awaited_once()
    agent_kwargs = mock_agent.await_args.kwargs
    assert agent_kwargs["trace_id"] == "trace-001"
    assert agent_kwargs["decision_lease_id"] == "lease-001"
    assert agent_kwargs["session_id"] == "session-001"
    assert agent_kwargs["state_version"] == 1
    assert agent_kwargs["session"]["sessionId"] == "session-001"

    mock_decision.assert_awaited_once()
    decision_kwargs = mock_decision.await_args.kwargs
    assert decision_kwargs["decision_url"] == "http://robotgateway.local/api/v1/hosting/llm/decision"
    assert decision_kwargs["trace_id"] == "trace-001"
    assert decision_kwargs["session_id"] == "session-001"
    assert decision_kwargs["decision_lease_id"] == "lease-001"
    assert decision_kwargs["state_version"] == 1
    assert decision_kwargs["recommended_action"]["skillName"] == "observe_state"
