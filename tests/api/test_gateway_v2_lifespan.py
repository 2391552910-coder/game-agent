from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.api import main
from src.api.routes import gateway_v2
from src.core.integration.llm_gateway_v2 import auth
from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchAck, parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.event_worker import (
    ClaimedGatewayEvent,
    EventProcessResult,
    EventWorker,
)
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry

APP_ID = "gateway-events"
APP_SECRET = "gateway-events-secret"
GATEWAY_ID = "gateway-1"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _headers(path: str, body: bytes) -> dict[str, str]:
    timestamp = "1700000000100"
    request_id = "request-matrix"
    signing_text = "\n".join(("POST", path, timestamp, request_id, hashlib.sha256(body).hexdigest()))
    signature = hmac.new(APP_SECRET.encode(), signing_text.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-AppId": APP_ID,
        "X-TimestampMs": timestamp,
        "X-RequestId": request_id,
        "X-Signature": signature,
    }


def _v1_payload() -> dict[str, Any]:
    return {
        "traceId": "v1-trace-1",
        "gatewayId": GATEWAY_ID,
        "contractVersion": "llm-gateway-http-v1",
        "sentAtMs": 1_700_000_000_002,
        "events": [
            {
                "eventId": "v1-event-1",
                "eventType": "observation_updated",
                "sessionId": "session-1",
                "stateVersion": 1,
                "decisionLeaseId": "v1-lease-1",
                "occurredAtMs": 1_700_000_000_001,
                "payload": {
                    "eventType": "observation_updated",
                    "reason": "state_changed",
                    "session": {
                        "sessionId": "session-1",
                        "accountId": "account-1",
                        "roleId": "role-1",
                        "sceneId": 1,
                        "state": "Running",
                        "position": {"x": 0, "y": 0, "z": 0},
                        "controllable": True,
                        "roleName": "role",
                        "runtimeObjectCatalog": {},
                    },
                    "availableSkills": [],
                    "skillArgumentHints": [],
                    "lastSkillResult": None,
                },
            }
        ],
    }


def _v2_payload() -> dict[str, Any]:
    return {
        "traceId": "v2-trace-1",
        "gatewayId": GATEWAY_ID,
        "contractVersion": "llm-gateway-http-v2",
        "sentAtMs": 1_700_000_000_002,
        "events": [
            {
                "eventId": "v2-event-1",
                "eventType": "session_started",
                "sessionId": "session-1",
                "controlGeneration": 1,
                "eventSequence": 1,
                "occurredAtMs": 1_700_000_000_001,
                "payload": {
                    "lease": {
                        "decisionLeaseId": "v2-lease-1",
                        "stateVersion": 1,
                        "leaseKind": "hosting_control",
                        "allowedDecisionActions": ["wait"],
                        "session": {"accountId": "account-1", "status": "active"},
                        "availableSkills": [],
                        "skillArgumentHints": [],
                    }
                },
            }
        ],
    }


def _configure(settings, *, v1_enabled: bool, v2_enabled: bool) -> None:
    settings.llm_gateway_v1_enabled = v1_enabled
    settings.llm_gateway_v2_enabled = v2_enabled
    settings.llm_gateway_app_secrets = {APP_ID: APP_SECRET}
    settings.llm_gateway_app_gateways = {APP_ID: [GATEWAY_ID]}
    settings.llm_gateway_app_tenants = {GATEWAY_ID: str(TENANT_ID)}
    settings.llm_gateway_timestamp_tolerance_ms = 10**15
    auth.settings = settings
    gateway_v2.settings = settings


async def _post(client, path: str, payload: dict[str, Any]):
    body = _json_bytes(payload)
    return await client.post(path, content=body, headers=_headers(path, body))


async def _assert_matrix(client, settings, *, v1_enabled: bool, v2_enabled: bool) -> None:
    _configure(settings, v1_enabled=v1_enabled, v2_enabled=v2_enabled)
    ack = GatewayV2BatchAck.model_validate(
        {
            "accepted": True,
            "traceId": "v2-trace-1",
            "receivedEventIds": ["v2-event-1"],
            "duplicateEventIds": [],
        }
    )
    with (
        patch("src.api.routes.webhooks.enqueue_gateway_event", AsyncMock(return_value="accepted")),
        patch("src.api.routes.gateway_v2.accept_gateway_event_batch", AsyncMock(return_value=ack)),
    ):
        v1_response = await _post(client, "/api/gateway/events", _v1_payload())
        v2_response = await _post(client, "/api/gateway/v2/events", _v2_payload())

    assert v1_response.status_code == (200 if v1_enabled else 503)
    assert v2_response.status_code == (200 if v2_enabled else 503)
    if not v1_enabled:
        assert v1_response.json()["error"]["code"] == "service_disabled"
    if not v2_enabled:
        assert v2_response.json()["error"]["code"] == "service_disabled"


@pytest.mark.asyncio
async def test_v1_on_v2_off_matrix(client, _mock_settings) -> None:
    await _assert_matrix(client, _mock_settings, v1_enabled=True, v2_enabled=False)


@pytest.mark.asyncio
async def test_v1_on_v2_on_matrix(client, _mock_settings) -> None:
    await _assert_matrix(client, _mock_settings, v1_enabled=True, v2_enabled=True)


@pytest.mark.asyncio
async def test_v1_off_v2_on_matrix(client, _mock_settings) -> None:
    await _assert_matrix(client, _mock_settings, v1_enabled=False, v2_enabled=True)


@pytest.mark.asyncio
async def test_v1_off_v2_off_matrix(client, _mock_settings) -> None:
    await _assert_matrix(client, _mock_settings, v1_enabled=False, v2_enabled=False)


@pytest.mark.asyncio
async def test_disabled_routes_remain_visible_in_openapi(client, _mock_settings) -> None:
    _configure(_mock_settings, v1_enabled=False, v2_enabled=False)

    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/api/gateway/events" in paths
    assert "/api/gateway/v2/events" in paths


@dataclass
class _LifecycleWorker:
    name: str
    operations: list[str]

    async def start(self) -> None:
        self.operations.append(f"{self.name}:start")

    async def drain(self) -> None:
        self.operations.append(f"{self.name}:drain")

    async def stop(self) -> None:
        self.operations.append(f"{self.name}:stop")


@dataclass
class _LifecycleReadiness:
    operations: list[str]

    def enable(self) -> None:
        self.operations.append("readiness:enable")

    def disable(self) -> None:
        self.operations.append("readiness:disable")


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_dependencies_in_contract_order(_mock_settings) -> None:
    operations: list[str] = []
    _mock_settings.llm_provider_source = "env"
    _mock_settings.llm_gateway_v1_enabled = True
    _mock_settings.llm_gateway_event_worker_enabled = True
    _mock_settings.llm_gateway_v2_enabled = True
    _mock_settings.llm_gateway_v2_shutdown_grace_seconds = 1
    main.settings = _mock_settings
    readiness = _LifecycleReadiness(operations)
    original_readiness = main.app.state.readiness_service
    main.app.state.readiness_service = readiness
    runtime = main.GatewayV2Runtime(
        event_worker=_LifecycleWorker("event", operations),
        decision_worker=_LifecycleWorker("decision", operations),
    )

    try:
        with (
            patch("src.api.main.init_db", AsyncMock(side_effect=lambda: operations.append("db:start"))),
            patch("src.api.main.init_redis", AsyncMock(side_effect=lambda: operations.append("redis:start"))),
            patch("src.api.main.close_db", AsyncMock(side_effect=lambda: operations.append("db:stop"))),
            patch("src.api.main.close_redis", AsyncMock(side_effect=lambda: operations.append("redis:stop"))),
            patch(
                "src.api.main.start_gateway_event_worker",
                AsyncMock(side_effect=lambda _processor: operations.append("v1:start")),
            ),
            patch(
                "src.api.main.stop_gateway_event_worker",
                AsyncMock(side_effect=lambda: operations.append("v1:stop")),
            ),
            patch("src.api.main.build_gateway_v2_runtime", MagicMock(return_value=runtime)),
        ):
            async with main.lifespan(main.app):
                operations.append("serving")
    finally:
        main.app.state.readiness_service = original_readiness

    assert operations == [
        "db:start",
        "redis:start",
        "v1:start",
        "event:start",
        "decision:start",
        "readiness:enable",
        "serving",
        "readiness:disable",
        "event:drain",
        "decision:drain",
        "v1:stop",
        "redis:stop",
        "db:stop",
    ]


@dataclass
class _ClaimRepository:
    claims: deque[ClaimedGatewayEvent | None]
    completions: list[EventProcessResult] = field(default_factory=list)

    async def sweep_expired_claims(self, *, max_attempts: int) -> int:
        del max_attempts
        return 0

    async def claim_next_event(self, **kwargs):
        del kwargs
        return self.claims.popleft() if self.claims else None

    async def count_dead_letters(self) -> int:
        return 0

    async def renew_event_claim(self, event, *, claim_ttl_ms: int) -> bool:
        del event, claim_ttl_ms
        return True

    async def complete_event(self, event, result, **kwargs) -> bool:
        del event, kwargs
        self.completions.append(result)
        return True


def _claimed_event() -> ClaimedGatewayEvent:
    event = parse_gateway_v2_event(_v2_payload()["events"][0])
    return ClaimedGatewayEvent(
        row_id=uuid4(),
        tenant_id=TENANT_ID,
        cycle_id=uuid4(),
        gateway_id=GATEWAY_ID,
        session_id="session-1",
        event_id="v2-event-1",
        event_type="session_started",
        control_generation=1,
        event_sequence=1,
        event=event,
        content_hash="a" * 64,
        trace_id="v2-trace-1",
        claim_token=uuid4(),
        claimed_fence_version=1,
        attempt_count=1,
        locked_by="worker-1",
        lock_until=datetime.now(UTC) + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_inflight_without_completing_claim() -> None:
    from src.api.main import GatewayV2Runtime, shutdown_gateway_v2_runtime

    entered = asyncio.Event()
    cancelled = asyncio.Event()
    repository = _ClaimRepository(deque([_claimed_event(), None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    event_worker = EventWorker(
        repository=repository,
        processor=processor,
        status_registry=WorkerStatusRegistry(),
        worker_id="event-worker",
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )
    idle_decision_worker = _LifecycleWorker("decision", [])
    await event_worker.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    await shutdown_gateway_v2_runtime(
        GatewayV2Runtime(event_worker=event_worker, decision_worker=idle_decision_worker),
        grace_seconds=0.01,
    )

    assert cancelled.is_set()
    assert repository.completions == []
    assert event_worker.status_snapshot().state == "stopped"
