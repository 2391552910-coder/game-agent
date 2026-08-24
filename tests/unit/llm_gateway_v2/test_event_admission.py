from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import UUID

import pytest

from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.canonical import event_content_hash
from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchEnvelope
from src.core.integration.llm_gateway_v2.event_admission import (
    EventAdmissionLimiter,
    EventAdmissionOverloadedError,
)
from src.core.integration.llm_gateway_v2.event_service import (
    EventService,
    EventServiceUnavailable,
)
from src.core.integration.llm_gateway_v2.inbox_repository import (
    BatchAcceptance,
    InboxRepository,
    _PreparedEvent,
)

IDENTITY = InboundGatewayIdentity(
    app_id="gateway-events",
    gateway_id="gateway-1",
    tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
)


def _envelope(event_id: str = "event-1") -> GatewayV2BatchEnvelope:
    return GatewayV2BatchEnvelope.model_validate(
        {
            "traceId": "trace-1",
            "gatewayId": "gateway-1",
            "contractVersion": "llm-gateway-http-v2",
            "sentAtMs": 1_700_000_000_100,
            "events": [
                {
                    "eventId": event_id,
                    "eventType": "session_started",
                    "sessionId": "session-1",
                    "controlGeneration": 1,
                    "eventSequence": 1,
                    "stateVersion": 1,
                    "decisionLeaseId": "lease-1",
                    "occurredAtMs": 1_700_000_000_001,
                    "payload": {
                        "reason": "decision_requested",
                        "lease": {
                            "sessionId": "session-1",
                            "controlGeneration": 1,
                            "decisionLeaseId": "lease-1",
                            "stateVersion": 1,
                            "leaseKind": "hosting_control",
                            "allowedActions": ["wait"],
                            "allowedSkillName": None,
                            "allowedSkillNames": [],
                            "parentSkillName": None,
                        },
                        "decisionContext": {
                            "session": {"status": "active"},
                            "availableSkills": [],
                            "skillArgumentHints": [],
                            "lastSkillResult": None,
                        },
                    },
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_event_admission_limiter_fails_fast_when_capacity_is_saturated() -> None:
    limiter = EventAdmissionLimiter(max_concurrency=1, acquire_timeout_seconds=0.01)

    async with limiter.slot():
        started = time.monotonic()
        with pytest.raises(EventAdmissionOverloadedError):
            async with limiter.slot():
                raise AssertionError("the saturated slot must not be entered")

    assert time.monotonic() - started < 0.2


@dataclass
class _BlockingRepository:
    entered: asyncio.Event
    release: asyncio.Event

    async def accept_event_batch(self, identity, trace_id, events):
        self.entered.set()
        await self.release.wait()
        return BatchAcceptance((events[0].event_id,), ())


@pytest.mark.asyncio
async def test_event_service_maps_saturated_admission_to_fast_unavailable() -> None:
    limiter = EventAdmissionLimiter(max_concurrency=1, acquire_timeout_seconds=0.01)
    repository = _BlockingRepository(asyncio.Event(), asyncio.Event())
    service = EventService(repository, admission_limiter=limiter)

    first = asyncio.create_task(service.accept_event_batch(IDENTITY, _envelope("event-1")))
    await repository.entered.wait()

    started = time.monotonic()
    with pytest.raises(EventServiceUnavailable):
        await service.accept_event_batch(IDENTITY, _envelope("event-2"))
    assert time.monotonic() - started < 0.2

    repository.release.set()
    await first


class _Savepoint:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _MappingResult:
    def mappings(self):
        return self

    def one(self):
        return {
            "runtime_session_id": "00000000-0000-0000-0000-000000000101",
            "cycle_id": "00000000-0000-0000-0000-000000000102",
            "event_id": "event-fast",
        }

    def scalars(self):
        return iter(("event-fast",))


class _FastAdmissionSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement, parameters):
        self.statements.append(statement.text)
        return _MappingResult()


@pytest.mark.asyncio
async def test_non_chat_event_admission_uses_one_database_statement() -> None:
    event = _envelope("event-fast").events[0]
    session = _FastAdmissionSession()
    repository = InboxRepository(session_factory=lambda: None)

    result = await repository._insert_non_chat_batch(
        session,
        IDENTITY,
        "trace-fast",
        (_PreparedEvent(event=event, content_hash=event_content_hash(IDENTITY.gateway_id, event)),),
    )

    assert result == frozenset({"event-fast"})
    assert len(session.statements) == 1
