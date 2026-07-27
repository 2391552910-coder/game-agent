from dataclasses import dataclass, field
from uuid import UUID

import pytest
from asyncpg.exceptions import NumericValueOutOfRangeError
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError

from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchEnvelope
from src.core.integration.llm_gateway_v2.event_service import (
    EventContentConflict,
    EventService,
    EventServiceUnavailable,
)
from src.core.integration.llm_gateway_v2.inbox_repository import (
    BatchAcceptance,
    ContentConflict,
    EventAdmissionConflict,
    EventAdmissionUnavailable,
    StoreUnavailable,
    _is_recoverable_event_statement_error,
)


def _event(event_id: str = "event-1", *, sequence: int = 1) -> dict:
    if sequence == 1:
        event_type = "session_started"
        payload = {
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
            },
        }
        decision_lease_id = "lease-1"
    else:
        event_type = "session_stopped"
        payload = {
            "reason": "stopped",
            "stoppedAtMs": 1_700_000_000_000 + sequence,
        }
        decision_lease_id = None
    return {
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": sequence,
        "stateVersion": 1,
        "decisionLeaseId": decision_lease_id,
        "occurredAtMs": 1_700_000_000_000 + sequence,
        "payload": payload,
    }


def _envelope(*events: dict, trace_id: str = "trace-1") -> GatewayV2BatchEnvelope:
    return GatewayV2BatchEnvelope.model_validate(
        {
            "traceId": trace_id,
            "gatewayId": "gateway-1",
            "contractVersion": "llm-gateway-http-v2",
            "sentAtMs": 1_700_000_000_100,
            "events": list(events or (_event(),)),
        }
    )


IDENTITY = InboundGatewayIdentity(
    app_id="gateway-events",
    gateway_id="gateway-1",
    tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
)


@dataclass
class StubRepository:
    results: list[BatchAcceptance | Exception]
    calls: list[tuple[InboundGatewayIdentity, str, tuple]] = field(default_factory=list)

    async def accept_event_batch(self, identity, trace_id, events):
        self.calls.append((identity, trace_id, tuple(events)))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_service_builds_exact_ack_in_first_seen_request_order() -> None:
    acceptance = BatchAcceptance(
        received_event_ids=("event-2", "event-1"),
        duplicate_event_ids=("event-3",),
    )
    repository = StubRepository([acceptance])
    service = EventService(repository)
    envelope = _envelope(
        _event("event-1"),
        _event("event-2", sequence=2),
        _event("event-3", sequence=3),
        _event("event-1"),
    )

    ack = await service.accept_event_batch(IDENTITY, envelope)

    assert ack.model_dump() == {
        "accepted": True,
        "traceId": "trace-1",
        "receivedEventIds": ["event-1", "event-2"],
        "duplicateEventIds": ["event-3"],
    }
    assert repository.calls == [(IDENTITY, "trace-1", envelope.events)]


@pytest.mark.parametrize(
    ("repository_error", "service_error"),
    [
        (EventAdmissionConflict("event-1"), EventContentConflict),
        (EventAdmissionUnavailable(), EventServiceUnavailable),
    ],
)
@pytest.mark.asyncio
async def test_service_maps_typed_repository_failures(
    repository_error: Exception,
    service_error: type[Exception],
) -> None:
    service = EventService(StubRepository([repository_error]))

    with pytest.raises(service_error) as caught:
        await service.accept_event_batch(IDENTITY, _envelope(_event()))

    assert caught.value.args == ()


@pytest.mark.asyncio
async def test_full_retry_after_response_loss_reaches_repository_again() -> None:
    repository = StubRepository(
        [
            BatchAcceptance(("event-1", "event-2"), ()),
            BatchAcceptance((), ("event-1", "event-2")),
        ]
    )
    service = EventService(repository)
    first_request = _envelope(_event("event-1"), _event("event-2", sequence=2))
    retry = _envelope(
        _event("event-1"),
        _event("event-2", sequence=2),
        trace_id="trace-retry",
    )

    first_ack = await service.accept_event_batch(IDENTITY, first_request)
    retry_ack = await service.accept_event_batch(IDENTITY, retry)

    assert first_ack.received_event_ids == ("event-1", "event-2")
    assert retry_ack.received_event_ids == ()
    assert retry_ack.duplicate_event_ids == ("event-1", "event-2")
    assert len(repository.calls) == 2


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "error",
    [
        IntegrityError("INSERT", {}, _DriverError("23505")),
        DataError("INSERT", {}, _DriverError("22003")),
        DBAPIError("INSERT", {}, NumericValueOutOfRangeError("numeric value out of range")),
    ],
)
def test_only_statement_local_integrity_and_data_failures_are_recoverable(error: DBAPIError) -> None:
    assert _is_recoverable_event_statement_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("INSERT", {}, _DriverError("40P01")),
        OperationalError("INSERT", {}, _DriverError("40001")),
        OperationalError("INSERT", {}, _DriverError("08006")),
        DBAPIError("INSERT", {}, _DriverError("22003")),
        DBAPIError("INSERT", {}, _DriverError("22003"), connection_invalidated=True),
    ],
)
def test_transaction_or_connection_failures_are_not_partially_recoverable(error: DBAPIError) -> None:
    assert _is_recoverable_event_statement_error(error) is False


def test_repository_exposes_plan_named_typed_errors() -> None:
    assert ContentConflict is EventAdmissionConflict
    assert StoreUnavailable is EventAdmissionUnavailable
