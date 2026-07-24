"""Gateway 侧真实外部决策 HTTP 契约测试。"""

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


def _session(*, pascal_case: bool = False) -> dict:
    values = {
        "sessionId": "session-001",
        "accountId": "account-001",
        "roleId": "role-001",
        "sceneId": 1001,
        "state": "Running",
        "position": {"x": 12.3, "y": 0.0, "z": 45.6},
        "controllable": True,
        "roleName": "测试角色",
        "runtimeObjectCatalog": {"role": {"roleId": "role-001"}},
    }
    if not pascal_case:
        return values
    return {
        "SessionId": values["sessionId"],
        "AccountId": values["accountId"],
        "RoleId": values["roleId"],
        "SceneId": values["sceneId"],
        "State": values["state"],
        "Position": {"X": 12.3, "Y": 0.0, "Z": 45.6},
        "Controllable": values["controllable"],
        "RoleName": values["roleName"],
        "RuntimeObjectCatalog": values["runtimeObjectCatalog"],
    }


def _payload(event_type: str, *, pascal_case_session: bool = False) -> dict:
    payload = {
        "eventType": event_type,
        "reason": "state_changed" if event_type == "observation_updated" else None,
        "session": _session(pascal_case=pascal_case_session),
        "availableSkills": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "requireRunning": True,
                "cooldownMs": 0,
                "exposure": {"enabled": True},
            },
            {
                "skillName": "move_to",
                "schemaVersion": "v1",
                "requireRunning": True,
                "cooldownMs": 0,
                "exposure": {"enabled": True},
            },
        ],
        "skillArgumentHints": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "argumentStatus": "ready",
                "allowedArgs": [],
                "missingArgs": [],
                "warnings": [],
                "stateRefs": [],
                "nextSteps": [],
            },
        ],
        "lastSkillResult": None,
    }
    if event_type == "skill_finished":
        payload["reason"] = "ok"
        payload["lastSkillResult"] = {
            "skillCallId": "skill-call-001",
            "skillName": "observe_state",
            "status": "success",
            "reason": "ok",
        }
    if event_type == "decision_rejected":
        payload["reason"] = "stale_state"
        payload["lastSkillResult"] = {
            "skillCallId": "skill-call-002",
            "skillName": "move_to",
            "status": "rejected",
            "reason": "stale_state",
        }
    return payload


def _event(event_type: str = "observation_updated", event_id: str = "evt-001", state_version: int = 1) -> dict:
    return {
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": "session-001",
        "stateVersion": state_version,
        "decisionLeaseId": f"lease-{state_version}",
        "occurredAtMs": 1719999999000 + state_version,
        "payload": _payload(event_type),
    }


def _event_batch(*events: dict, trace_id: str = "trace-001") -> dict:
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
async def test_gateway_accepts_actual_batch_and_returns_ack(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    payload = _event_batch(_event("observation_updated", "evt-observe"), _event("skill_finished", "evt-finished", 2))

    with patch("src.api.routes.webhooks.enqueue_gateway_event", AsyncMock(return_value="accepted")) as enqueue:
        response = await _post_gateway_payload(client, payload)

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "traceId": "trace-001",
        "receivedEventIds": ["evt-observe", "evt-finished"],
        "duplicateEventIds": [],
    }
    assert enqueue.await_count == 2


@pytest.mark.asyncio
async def test_gateway_accepts_all_current_event_types(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    payload = _event_batch(
        _event("observation_updated", "evt-observe", 1),
        _event("decision_rejected", "evt-rejected", 2),
        _event("skill_finished", "evt-finished", 3),
    )

    with patch("src.api.routes.webhooks.enqueue_gateway_event", AsyncMock(return_value="accepted")):
        response = await _post_gateway_payload(client, payload)

    assert response.status_code == 200
    assert response.json()["receivedEventIds"] == ["evt-observe", "evt-rejected", "evt-finished"]


@pytest.mark.asyncio
async def test_gateway_returns_ack_without_running_agent_in_request_handler(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    payload = _event_batch(_event())

    with (
        patch("src.api.routes.webhooks.enqueue_gateway_event", AsyncMock(return_value="accepted")),
        patch("src.api.routes.webhooks.run_gateway_v1_agent", AsyncMock()) as agent,
        patch("src.api.routes.webhooks.send_llm_gateway_decision", AsyncMock()) as decision,
    ):
        response = await _post_gateway_payload(client, payload)

    assert response.status_code == 200
    agent.assert_not_awaited()
    decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_accepts_gateway_pascal_case_session_snapshot(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    event = _event()
    event["payload"] = _payload("observation_updated", pascal_case_session=True)

    with patch("src.api.routes.webhooks.enqueue_gateway_event", AsyncMock(return_value="accepted")):
        response = await _post_gateway_payload(client, _event_batch(event))

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_gateway_rejects_event_types_not_currently_emitted(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    response = await _post_gateway_payload(client, _event_batch(_event("session_started")))
    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_rejects_payload_event_type_mismatch(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    event = _event("observation_updated")
    event["payload"]["eventType"] = "skill_finished"
    response = await _post_gateway_payload(client, _event_batch(event))
    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_rejects_missing_lease_for_current_event(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    event = _event()
    del event["decisionLeaseId"]
    response = await _post_gateway_payload(client, _event_batch(event))
    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_signature(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    body = _body_bytes(_event_batch(_event()))
    headers = _signed_headers("/api/gateway/events", body)
    headers["X-Signature"] = "0" * 64
    response = await client.post("/api/gateway/events", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "signature_invalid", "message": "request signature invalid"}
    }


@pytest.mark.asyncio
async def test_gateway_duplicate_and_conflict_are_reported_per_event(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    payload = _event_batch(_event())

    with patch(
        "src.api.routes.webhooks.enqueue_gateway_event",
        AsyncMock(side_effect=["accepted", "duplicate"]),
    ):
        first = await _post_gateway_payload(client, payload)
        second = await _post_gateway_payload(client, payload, request_id="req-002")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["receivedEventIds"] == ["evt-001"]
    assert second.json()["duplicateEventIds"] == ["evt-001"]


@pytest.mark.asyncio
async def test_gateway_conflict_is_request_error(client, _mock_settings):
    _configure_gateway_settings(_mock_settings)
    payload = _event_batch(_event())
    with patch("src.api.routes.webhooks.enqueue_gateway_event", AsyncMock(return_value="conflict")):
        response = await _post_gateway_payload(client, payload)
    _assert_bad_request(response)


@pytest.mark.asyncio
async def test_gateway_worker_passes_full_event_context_to_agent(_mock_settings):
    from src.api.routes.webhooks import process_gateway_event_record

    _configure_gateway_settings(_mock_settings)
    _mock_settings.llm_gateway_decision_url = "http://robotgateway.local/api/v1/hosting/llm/decision"
    _mock_settings.llm_gateway_decision_app_id = "llm-to-gateway"
    _mock_settings.llm_gateway_decision_app_secret = "secret-llm"
    event = _event("skill_finished")
    agent_output = {
        "recommended_actions": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "arguments": {},
                "reason": "观察",
                "priority": "high",
                "ttlMs": 3000,
            }
        ]
    }
    with (
        patch("src.api.routes.webhooks.run_gateway_v1_agent", AsyncMock(return_value=agent_output)) as agent,
        patch(
            "src.api.routes.webhooks.send_llm_gateway_decision",
            AsyncMock(return_value={"accepted": True, "status": "accepted", "reason": "ok"}),
        ) as decision,
    ):
        await process_gateway_event_record(
            {
                "traceId": "trace-001",
                "gatewayId": "gateway-01",
                "contractVersion": "llm-gateway-http-v1",
                "event": event,
            }
        )

    agent_kwargs = agent.await_args.kwargs
    assert agent_kwargs["event_type"] == "skill_finished"
    assert agent_kwargs["event_payload"]["lastSkillResult"]["reason"] == "ok"
    assert agent_kwargs["event_payload"]["availableSkills"][0]["skillName"] == "observe_state"
    decision_kwargs = decision.await_args.kwargs
    assert decision_kwargs["session_id"] == "session-001"
    assert decision_kwargs["decision_lease_id"] == "lease-1"
    assert decision_kwargs["state_version"] == 1


def test_gateway_action_selection_rejects_skill_not_advertised():
    from src.api.routes.webhooks import GatewayEventPayload, _select_gateway_action

    payload = GatewayEventPayload.model_validate(_payload("observation_updated"))
    payload.available_skills = [payload.available_skills[0]]

    selected = _select_gateway_action(
        [
            {
                "skillName": "move_to",
                "schemaVersion": "v1",
                "arguments": {"target": {"x": 1, "y": 0, "z": 2}},
                "reason": "不应发送",
            }
        ],
        payload,
    )

    assert selected["action"] == "wait"
    assert selected["arguments"] == {"waitMs": 1000}
