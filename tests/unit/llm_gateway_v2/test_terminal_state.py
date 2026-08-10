from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.event_service import GatewayV2EventDispatcher
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent, EventProcessResult
from src.core.integration.llm_gateway_v2.outbox_repository import (
    resolve_decision_rejection,
    session_stop_skill_status,
)
from src.core.integration.llm_gateway_v2.terminal_effect_service import tracking_status_for_terminal
from src.core.integration.llm_gateway_v2.terminal_repository import (
    MutationDisposition,
    MutationResult,
    TerminalRecord,
    TerminalRepository,
    normalize_skill_terminal,
    resolve_terminal_transition,
)


def _lease() -> dict[str, Any]:
    return {
        "sessionId": "session-1",
        "controlGeneration": 1,
        "decisionLeaseId": "lease-next",
        "stateVersion": 2,
        "leaseKind": "observation",
        "allowedActions": ["wait"],
        "allowedSkillName": None,
        "allowedSkillNames": [],
        "parentSkillName": None,
    }


def _decision_context() -> dict[str, Any]:
    return {
        "session": {"status": "active"},
        "availableSkills": [],
        "skillArgumentHints": [],
        "lastSkillResult": None,
    }


def _claimed(
    event_type: str,
    *,
    terminal: dict[str, Any] | None = None,
    with_lease: bool = False,
    reason: str = "session ended",
) -> ClaimedGatewayEvent:
    sequence_by_type = {
        "session_started": 1,
        "observation_updated": 2,
        "skill_started": 3,
        "skill_finished": 4,
        "decision_rejected": 5,
        "session_stopped": 6,
    }
    sequence = sequence_by_type[event_type]
    occurred_at_ms = 1_700_000_000_000 + sequence
    terminal_payload = terminal or {"status": "success"}
    decision_payload = {
        "reason": "decision_requested",
        "lease": _lease(),
        "decisionContext": _decision_context(),
    }
    payload_by_type: dict[str, dict[str, Any]] = {
        "session_started": decision_payload,
        "observation_updated": decision_payload,
        "skill_started": {
            "decisionId": "decision-1",
            "skillName": "jump",
            "skillCallId": "call-1",
            "startedAtMs": occurred_at_ms,
        },
        "skill_finished": {
            "decisionId": "decision-1",
            "skillName": "jump",
            "skillCallId": "call-1",
            "status": terminal_payload["status"],
            "reason": terminal_payload.get("reason", "ok"),
            "failureCategory": terminal_payload.get("failureCategory"),
            "retryable": terminal_payload.get("retryable", False),
            "startedAtMs": occurred_at_ms - 1,
            "finishedAtMs": occurred_at_ms,
        },
        "decision_rejected": {
            "decisionId": "decision-1",
            "action": "call_skill",
            "skillName": "jump",
            "reason": "lease expired",
            "rejectedAtMs": occurred_at_ms,
        },
        "session_stopped": {"reason": reason, "stoppedAtMs": occurred_at_ms},
    }
    if event_type == "skill_finished" and with_lease:
        payload_by_type[event_type]["lease"] = _lease()
        payload_by_type[event_type]["decisionContext"] = _decision_context()
    has_lease = event_type in {"session_started", "observation_updated"} or (
        event_type == "skill_finished" and with_lease
    )
    event = parse_gateway_v2_event(
        {
            "eventId": f"event-{event_type}",
            "eventType": event_type,
            "sessionId": "session-1",
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": 2,
            "decisionLeaseId": "lease-next" if has_lease else None,
            "occurredAtMs": occurred_at_ms,
            "payload": payload_by_type[event_type],
        }
    )
    now = datetime.now(UTC)
    return ClaimedGatewayEvent(
        row_id=uuid4(),
        tenant_id=UUID("00000000-0000-0000-0000-000000000073"),
        cycle_id=uuid4(),
        gateway_id="gateway-1",
        session_id="session-1",
        event_id=event.event_id,
        event_type=event.event_type,
        control_generation=1,
        event_sequence=sequence,
        event=event,
        content_hash="a" * 64,
        trace_id="trace-1",
        claim_token=uuid4(),
        claimed_fence_version=1,
        attempt_count=1,
        locked_by="worker-1",
        lock_until=now + timedelta(seconds=30),
    )


@dataclass
class _ContextRepository:
    operations: list[str]
    result: bool = True

    async def persist_lease_context(self, event, context) -> bool:
        del event, context
        self.operations.append("context")
        return self.result


@dataclass
class _TerminalRepository:
    operations: list[str]
    result: MutationResult = MutationResult(MutationDisposition.APPLIED)

    async def record_skill_started(self, event) -> MutationResult:
        del event
        self.operations.append("started")
        return self.result

    async def record_skill_finished(self, event) -> MutationResult:
        del event
        self.operations.append("terminal")
        return self.result


@dataclass
class _OutboxRepository:
    operations: list[str]
    result: MutationResult = MutationResult(MutationDisposition.APPLIED)

    async def merge_decision_rejected(self, event) -> MutationResult:
        del event
        self.operations.append("rejected")
        return self.result

    async def close_generation(self, event) -> MutationResult:
        del event
        self.operations.append("stopped")
        return self.result


@dataclass
class _ActivityRepository:
    operations: list[str]

    async def record_skill_started(self, event) -> bool:
        del event
        self.operations.append("activity_started")
        return True

    async def record_skill_finished(self, event) -> bool:
        del event
        self.operations.append("activity_terminal")
        return True

    async def record_decision_rejected(self, event) -> bool:
        del event
        self.operations.append("activity_rejected")
        return True

    async def record_chat_opportunity(self, event) -> bool:
        del event
        self.operations.append("activity_chat")
        return True

    async def close(self, event) -> bool:
        del event
        self.operations.append("activity_closed")
        return True


@dataclass
class _Planner:
    operations: list[str]
    result: EventProcessResult = EventProcessResult("succeeded")
    events: list[str] = field(default_factory=list)

    async def __call__(self, event, context) -> EventProcessResult:
        del context
        self.operations.append("agent")
        self.events.append(event.event_type)
        return self.result


def _dispatcher(
    operations: list[str],
    *,
    context_result: bool = True,
    terminal_result: MutationResult | None = None,
    outbox_result: MutationResult | None = None,
    with_activity: bool = False,
) -> tuple[GatewayV2EventDispatcher, _Planner]:
    planner = _Planner(operations)
    terminal_result = terminal_result or MutationResult(MutationDisposition.APPLIED)
    outbox_result = outbox_result or MutationResult(MutationDisposition.APPLIED)
    return (
        GatewayV2EventDispatcher(
            context_repository=_ContextRepository(operations, result=context_result),
            terminal_repository=_TerminalRepository(operations, result=terminal_result),
            outbox_repository=_OutboxRepository(operations, result=outbox_result),
            decision_planner=planner,
            activity_repository=(
                _ActivityRepository(operations) if with_activity else None
            ),
        ),
        planner,
    )


@pytest.mark.parametrize("event_type", ["session_started", "observation_updated"])
async def test_lease_events_persist_context_before_agent(event_type: str) -> None:
    operations: list[str] = []
    dispatcher, planner = _dispatcher(operations)

    result = await dispatcher(_claimed(event_type))

    assert result == EventProcessResult("succeeded")
    assert operations == ["context", "agent"]
    assert planner.events == [event_type]


async def test_skill_started_only_upserts_started_call() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations)

    assert await dispatcher(_claimed("skill_started")) == EventProcessResult("succeeded")
    assert operations == ["started"]


async def test_skill_started_records_activity_after_terminal_state() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations, with_activity=True)

    assert await dispatcher(_claimed("skill_started")) == EventProcessResult("succeeded")
    assert operations == ["started", "activity_started"]


async def test_skill_finished_without_lease_only_converges_terminal() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations)

    assert await dispatcher(_claimed("skill_finished")) == EventProcessResult("succeeded")
    assert operations == ["terminal"]


async def test_skill_finished_with_lease_converges_terminal_before_agent() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations)

    assert await dispatcher(_claimed("skill_finished", with_lease=True)) == EventProcessResult("succeeded")
    assert operations == ["terminal", "context", "agent"]


async def test_skill_finished_advances_activity_before_next_decision() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations, with_activity=True)

    result = await dispatcher(_claimed("skill_finished", with_lease=True))

    assert result == EventProcessResult("succeeded")
    assert operations == ["terminal", "activity_terminal", "context", "agent"]


async def test_historical_skill_finished_never_reactivates_agent_cycle() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations)
    historical = replace(
        _claimed("skill_finished", with_lease=True),
        historical_recovery=True,
    )

    assert await dispatcher(historical) == EventProcessResult("succeeded")
    assert operations == ["terminal"]


async def test_historical_skill_finished_does_not_advance_current_activity() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations, with_activity=True)
    historical = replace(
        _claimed("skill_finished", with_lease=True),
        historical_recovery=True,
    )

    assert await dispatcher(historical) == EventProcessResult("succeeded")
    assert operations == ["terminal"]


async def test_decision_rejected_only_merges_rejection() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations)

    assert await dispatcher(_claimed("decision_rejected")) == EventProcessResult("succeeded")
    assert operations == ["rejected"]


async def test_decision_rejected_updates_activity_after_outbox() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations, with_activity=True)

    assert await dispatcher(_claimed("decision_rejected")) == EventProcessResult("succeeded")
    assert operations == ["rejected", "activity_rejected"]


async def test_session_stopped_only_closes_generation() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations)

    assert await dispatcher(_claimed("session_stopped")) == EventProcessResult("succeeded")
    assert operations == ["stopped"]


async def test_session_stopped_closes_activity_before_generation() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations, with_activity=True)

    assert await dispatcher(_claimed("session_stopped")) == EventProcessResult("succeeded")
    assert operations == ["activity_closed", "stopped"]


async def test_lost_lease_context_fence_does_not_run_agent() -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(operations, context_result=False)

    result = await dispatcher(_claimed("observation_updated"))

    assert result == EventProcessResult("manual", error_stage="fence", error_category="claim_lost")
    assert operations == ["context"]


@pytest.mark.parametrize(
    ("disposition", "outcome", "category"),
    [
        (MutationDisposition.IDEMPOTENT, "succeeded", None),
        (MutationDisposition.CONFLICT, "manual", "state_conflict"),
        (MutationDisposition.MISSING, "retryable_failed", "missing_dependency"),
        (MutationDisposition.FENCED, "manual", "claim_lost"),
    ],
)
async def test_dispatcher_maps_durable_mutation_results(
    disposition: MutationDisposition,
    outcome: str,
    category: str | None,
) -> None:
    operations: list[str] = []
    dispatcher, _ = _dispatcher(
        operations,
        terminal_result=MutationResult(disposition),
    )

    result = await dispatcher(_claimed("skill_finished"))

    assert result.outcome == outcome
    assert result.error_category == category


@pytest.mark.parametrize(
    "terminal",
    [
        {"status": "success"},
        {
            "status": "failed",
            "failureCategory": "business_rejected",
            "reason": "not allowed",
            "retryable": False,
        },
        {
            "status": "failed",
            "failureCategory": "transport_failed",
            "reason": "gateway unavailable",
            "retryable": True,
        },
        {
            "status": "failed",
            "failureCategory": "protocol_failed",
            "reason": "invalid response",
            "retryable": False,
        },
        {
            "status": "failed",
            "failureCategory": "internal_failed",
            "reason": "execution failed",
            "retryable": True,
        },
        {"status": "cancelled", "reason": "superseded", "retryable": False},
        {"status": "timeout", "reason": "deadline", "retryable": True},
    ],
)
def test_first_terminal_applies_once_for_every_terminal_shape(terminal: dict[str, Any]) -> None:
    incoming = normalize_skill_terminal(_claimed("skill_finished", terminal=terminal).event.payload.terminal)
    existing = TerminalRecord.pending()

    transition = resolve_terminal_transition(existing, incoming)

    assert transition.disposition is MutationDisposition.APPLIED
    assert transition.record == incoming


@pytest.mark.parametrize("same_event_id", [True, False])
def test_repeated_identical_terminal_is_idempotent(same_event_id: bool) -> None:
    incoming = normalize_skill_terminal(
        _claimed(
            "skill_finished",
            terminal={"status": "cancelled", "reason": "superseded", "retryable": False},
        ).event.payload.terminal
    )
    existing = incoming.with_terminal_event_id("event-1")
    repeated = incoming.with_terminal_event_id("event-1" if same_event_id else "event-2")

    transition = resolve_terminal_transition(existing, repeated)

    assert transition.disposition is MutationDisposition.IDEMPOTENT
    assert transition.record == existing


def test_conflicting_terminal_enters_manual_without_replacing_first_result() -> None:
    success = normalize_skill_terminal(_claimed("skill_finished").event.payload.terminal).with_terminal_event_id(
        "event-1"
    )
    failed = normalize_skill_terminal(
        _claimed(
            "skill_finished",
            terminal={
                "status": "failed",
                "failureCategory": "internal_failed",
                "reason": "late failure",
                "retryable": True,
            },
        ).event.payload.terminal
    ).with_terminal_event_id("event-2")

    transition = resolve_terminal_transition(success, failed)

    assert transition.disposition is MutationDisposition.CONFLICT
    assert transition.record.status == "manual"
    assert transition.record.terminal_event_id == "event-1"
    assert transition.record.reason == "terminal_conflict"
    assert transition.record.retryable is False


@pytest.mark.parametrize("reason", ["completion_unconfirmed", "vehicle_completion_unconfirmed"])
def test_unconfirmed_completion_never_retries_original_action(reason: str) -> None:
    terminal = _claimed(
        "skill_finished",
        terminal={"status": "timeout", "reason": reason, "retryable": True},
    ).event.payload.terminal

    normalized = normalize_skill_terminal(terminal)

    assert normalized.retryable is False


def test_terminal_call_identity_includes_session_skill_and_decision() -> None:
    decision = {"id": "decision-row-1", "decision_id": "decision-1"}
    call = {
        "decision_row_id": "decision-row-1",
        "decision_id": "decision-1",
        "session_id": "session-1",
        "skill_name": "move_to",
    }

    assert TerminalRepository._call_matches_identity(
        call,
        decision,
        session_id="session-1",
        skill_name="move_to",
    )
    for field_name, wrong_value in (
        ("decision_row_id", "decision-row-2"),
        ("decision_id", "decision-2"),
        ("session_id", "session-2"),
        ("skill_name", "jump"),
    ):
        changed = {**call, field_name: wrong_value}
        assert not TerminalRepository._call_matches_identity(
            changed,
            decision,
            session_id="session-1",
            skill_name="move_to",
        )


@pytest.mark.parametrize(
    ("terminal_status", "tracking_status"),
    [
        ("succeeded", "completed"),
        ("failed", "abandoned"),
        ("cancelled", "abandoned"),
        ("timeout", "timeout"),
    ],
)
def test_terminal_effect_has_a_deterministic_tracking_transition(
    terminal_status: str,
    tracking_status: str,
) -> None:
    assert tracking_status_for_terminal(terminal_status) == tracking_status


@pytest.mark.parametrize("current_status", ["planned", "sending"])
def test_pending_decision_rejection_becomes_rejected(current_status: str) -> None:
    resolution = resolve_decision_rejection(current_status, None, "lease expired")

    assert resolution.disposition is MutationDisposition.APPLIED
    assert resolution.status == "rejected"
    assert resolution.reason == "lease expired"


def test_repeated_rejection_preserves_first_reason_and_records_mismatch() -> None:
    same = resolve_decision_rejection("rejected", "lease expired", "lease expired")
    different = resolve_decision_rejection("rejected", "lease expired", "stale state")

    assert same.disposition is MutationDisposition.IDEMPOTENT
    assert different.disposition is MutationDisposition.IDEMPOTENT
    assert different.reason == "lease expired"
    assert different.error_category == "rejection_reason_mismatch"


def test_rejection_after_acceptance_is_manual_conflict() -> None:
    resolution = resolve_decision_rejection("accepted", "accepted", "lease expired")

    assert resolution.disposition is MutationDisposition.CONFLICT
    assert resolution.status == "manual"
    assert resolution.error_category == "rejected_after_accepted"


@pytest.mark.parametrize(
    ("action", "reason", "expected"),
    [
        ("stop_hosting", "stop_hosting_requested", "succeeded"),
        ("stop_hosting", "admin_stop", "cancelled"),
        ("call_skill", "stop_hosting_requested", "cancelled"),
        ("call_skill", "runtime_error", "cancelled"),
    ],
)
def test_session_stop_only_completes_matching_stop_hosting_call(
    action: str,
    reason: str,
    expected: str,
) -> None:
    assert session_stop_skill_status(action, reason) == expected
