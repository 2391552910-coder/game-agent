import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from src.api.routes import gateway_v2
from src.core.integration.llm_gateway_v2 import auth
from src.core.integration.llm_gateway_v2.auth import GatewayAuthError, InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchAck
from src.core.integration.llm_gateway_v2.event_service import EventContentConflict, EventServiceUnavailable

APP_ID = "gateway-events"
APP_SECRET = "gateway-events-secret"
GATEWAY_ID = "gateway-1"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
PATH = "/api/gateway/v2/events"


def _event(event_id: str = "event-1", *, sequence: int = 1) -> dict:
    if sequence == 1:
        event_type = "session_started"
        payload = {
            "lease": {
                "decisionLeaseId": "lease-1",
                "stateVersion": 1,
                "leaseKind": "hosting_control",
                "allowedDecisionActions": ["wait"],
                "session": {"status": "active"},
                "availableSkills": [],
                "skillArgumentHints": [],
            }
        }
    else:
        event_type = "session_stopped"
        payload = {"reason": "stopped"}
    return {
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": sequence,
        "occurredAtMs": 1_700_000_000_000 + sequence,
        "payload": payload,
    }


def _payload(*events: dict) -> dict:
    return {
        "traceId": "trace-1",
        "gatewayId": GATEWAY_ID,
        "contractVersion": "llm-gateway-http-v2",
        "sentAtMs": 1_700_000_000_100,
        "events": list(events or (_event(),)),
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _headers(body: bytes, request_id: str = "request-1") -> dict[str, str]:
    timestamp = "1700000000100"
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join(("POST", PATH, timestamp, request_id, body_hash))
    signature = hmac.new(APP_SECRET.encode(), signing_text.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-AppId": APP_ID,
        "X-TimestampMs": timestamp,
        "X-RequestId": request_id,
        "X-Signature": signature,
    }


def _configure(settings) -> None:
    settings.llm_gateway_v2_enabled = True
    settings.llm_gateway_app_secrets = {APP_ID: APP_SECRET}
    settings.llm_gateway_app_gateways = {APP_ID: [GATEWAY_ID]}
    settings.llm_gateway_app_tenants = {GATEWAY_ID: str(TENANT_ID)}
    settings.llm_gateway_timestamp_tolerance_ms = 10**15
    settings.llm_gateway_v2_max_event_batch_size = 2
    auth.settings = settings
    gateway_v2.settings = settings


async def _post(client, payload: dict, request_id: str = "request-1"):
    body = _body(payload)
    return await client.post(PATH, content=body, headers=_headers(body, request_id))


@pytest.mark.asyncio
async def test_valid_signed_batch_returns_exact_ack_without_tenant_api_key(client, _mock_settings) -> None:
    _configure(_mock_settings)
    ack = GatewayV2BatchAck.model_validate(
        {
            "accepted": True,
            "traceId": "trace-1",
            "receivedEventIds": ["event-1"],
            "duplicateEventIds": [],
        }
    )
    with patch(
        "src.api.routes.gateway_v2.accept_gateway_event_batch",
        AsyncMock(return_value=ack),
    ) as accept:
        response = await _post(client, _payload())

    assert response.status_code == 200
    assert response.json() == ack.model_dump()
    identity, envelope = accept.await_args.args
    assert identity == InboundGatewayIdentity(APP_ID, GATEWAY_ID, TENANT_ID)
    assert envelope.trace_id == "trace-1"


@pytest.mark.asyncio
async def test_hmac_is_checked_before_parsing_malformed_body(client, _mock_settings) -> None:
    _configure(_mock_settings)
    body = b"not-json"
    headers = _headers(body)
    headers["X-Signature"] = "0" * 64
    with (
        patch("src.api.routes.gateway_v2.resolve_inbound_identity") as resolve,
        patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept,
    ):
        response = await client.post(PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "signature_invalid"
    resolve.assert_not_called()
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_happens_before_identity_resolution(client, _mock_settings) -> None:
    _configure(_mock_settings)
    body = b"not-json"
    with (
        patch("src.api.routes.gateway_v2.verify_inbound_hmac", return_value=APP_ID),
        patch("src.api.routes.gateway_v2.resolve_inbound_identity") as resolve,
        patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept,
    ):
        response = await client.post(PATH, content=body, headers=_headers(body))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    resolve.assert_not_called()
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_tenant_identity_never_reaches_repository(client, _mock_settings) -> None:
    _configure(_mock_settings)
    with (
        patch(
            "src.api.routes.gateway_v2.resolve_inbound_identity",
            side_effect=GatewayAuthError("tenant_not_configured", 400),
        ) as resolve,
        patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept,
    ):
        response = await _post(client, _payload())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "tenant_not_configured"
    resolve.assert_called_once_with(APP_ID, GATEWAY_ID)
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_resolution_precedes_max_batch_rejection(client, _mock_settings) -> None:
    _configure(_mock_settings)
    oversized = _payload(_event("event-1"), _event("event-2", sequence=2), _event("event-3", sequence=2))
    identity = InboundGatewayIdentity(APP_ID, GATEWAY_ID, TENANT_ID)
    with (
        patch("src.api.routes.gateway_v2.resolve_inbound_identity", return_value=identity) as resolve,
        patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept,
    ):
        response = await _post(client, oversized)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    resolve.assert_called_once_with(APP_ID, GATEWAY_ID)
    accept.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (EventContentConflict(), 409, "event_content_conflict"),
        (EventServiceUnavailable(), 503, "service_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_repository_failures_have_stable_protocol_responses(
    client,
    _mock_settings,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    _configure(_mock_settings)
    with patch(
        "src.api.routes.gateway_v2.accept_gateway_event_batch",
        AsyncMock(side_effect=error),
    ):
        response = await _post(client, _payload())

    assert response.status_code == status_code
    assert response.json() == {"error": {"code": code, "message": response.json()["error"]["message"]}}


@pytest.mark.asyncio
async def test_capabilities_are_disabled_when_v2_is_off(client, _mock_settings) -> None:
    _mock_settings.llm_gateway_v2_enabled = False
    gateway_v2.settings = _mock_settings
    response = await client.get("/api/gateway/v2/capabilities")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_disabled"


@pytest.mark.asyncio
async def test_signed_non_json_content_type_is_bad_request(client, _mock_settings) -> None:
    _configure(_mock_settings)
    payload = _payload()
    body = _body(payload)
    headers = _headers(body)
    headers["Content-Type"] = "text/plain"
    with patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept:
        response = await client.post(PATH, content=body, headers=headers)

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "bad_request", "message": "bad request"}}
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_v2_route_is_stably_unavailable(client, _mock_settings) -> None:
    _configure(_mock_settings)
    _mock_settings.llm_gateway_v2_enabled = False
    with patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept:
        response = await _post(client, _payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_disabled"
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_v2_route_still_authenticates_before_feature_gate(client, _mock_settings) -> None:
    _configure(_mock_settings)
    _mock_settings.llm_gateway_v2_enabled = False
    body = b"not-json"
    headers = _headers(body)
    headers["X-Signature"] = "0" * 64

    with patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock()) as accept:
        response = await client.post(PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "signature_invalid"
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_failure_returns_and_logs_sanitized_internal_error(
    client,
    _mock_settings,
    caplog,
) -> None:
    _configure(_mock_settings)
    caplog.set_level("ERROR", logger="src.api.routes.gateway_v2")
    with patch(
        "src.api.routes.gateway_v2.accept_gateway_event_batch",
        AsyncMock(side_effect=RuntimeError("secret database detail")),
    ):
        response = await _post(client, _payload())

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "internal error"}}
    assert "secret database detail" not in response.text
    assert "secret database detail" not in caplog.text
    assert "LLM Gateway v2 event admission failed" in caplog.text


def test_v2_events_route_is_registered_at_stable_path() -> None:
    from src.api.main import app

    schema = app.openapi()
    assert PATH in schema["paths"]
    assert "/api/gateway/events" in schema["paths"]
    operation = schema["paths"][PATH]["post"]
    assert operation["tags"] == ["gateway-v2"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "X-AppId",
        "X-TimestampMs",
        "X-RequestId",
        "X-Signature",
    }
    assert all(parameter["required"] is True for parameter in operation["parameters"])
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["properties"]["contractVersion"]["const"] == "llm-gateway-http-v2"
    assert {"200", "400", "401", "409", "500", "503"}.issubset(operation["responses"])
