from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.integration.llm_gateway_v2 import inbox_repository
from src.core.integration.llm_gateway_v2.activity_plan import create_plaza_social_plan
from src.core.integration.llm_gateway_v2.activity_plan_repository import ActivityPlanRepository
from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event


class _ScalarSession:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def scalar(self, statement, parameters):
        self.calls.append((statement, parameters))
        return self.value


def _chat_event():
    return parse_gateway_v2_event(
        {
            "eventId": "chat-1",
            "eventType": "chat_received",
            "sessionId": "session-1",
            "stateVersion": 0,
            "decisionLeaseId": None,
            "occurredAtMs": 10,
            "payload": {
                "sessionId": "session-1",
                "schemaVersion": "v1",
                "contentType": 0,
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "private",
                "supported": True,
                "text": "你好",
                "serverTimeMs": 10,
            },
        }
    )


def _session_started_event():
    return parse_gateway_v2_event(
        {
            "eventId": "start-1",
            "eventType": "session_started",
            "sessionId": "session-1",
            "controlGeneration": 7,
            "eventSequence": 1,
            "stateVersion": 1,
            "decisionLeaseId": "lease-1",
            "occurredAtMs": 1,
            "payload": {
                "reason": "decision_requested",
                "lease": {
                    "sessionId": "session-1",
                    "controlGeneration": 7,
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
    )


def test_prepare_batch_inserts_sequenced_events_before_hosted_chat() -> None:
    chat = _chat_event()
    started = _session_started_event()

    prepared = inbox_repository._prepare_batch("gateway-1", (chat, started))
    orderer = getattr(inbox_repository, "_order_prepared_events_for_insertion", None)

    assert orderer is not None
    ordered = orderer(prepared)
    assert [item.event.event_id for item in ordered] == ["start-1", "chat-1"]


def test_hosted_chat_internal_sequence_uses_reserved_range() -> None:
    allocator = getattr(inbox_repository, "_next_hosted_chat_storage_sequence", None)

    assert allocator is not None
    assert allocator(None) == 2**62
    assert allocator(2**62) == 2**62 + 1
    with pytest.raises(inbox_repository.EventAdmissionUnavailable):
        allocator(2**63 - 1)


def test_hosted_chat_manual_failure_does_not_mark_decision_cycle_manual() -> None:
    predicate = getattr(inbox_repository, "_should_mark_cycle_manual", None)

    assert predicate is not None
    assert predicate("chat_received") is False
    assert predicate("nearby_friend_chat_requested") is False
    assert predicate("chat_send_result") is False
    assert predicate("observation_updated") is True


@pytest.mark.parametrize("cycle_status", ["pending", "active"])
def test_hosted_chat_is_processable_only_during_live_cycle(cycle_status: str) -> None:
    predicate = getattr(inbox_repository, "_hosted_chat_cycle_is_processable", None)

    assert predicate is not None
    assert predicate("chat_received", cycle_status) is True
    assert predicate("observation_updated", cycle_status) is True


@pytest.mark.parametrize("cycle_status", ["stopped", "superseded", "manual"])
def test_hosted_chat_is_not_processable_after_cycle_ends(cycle_status: str) -> None:
    predicate = getattr(inbox_repository, "_hosted_chat_cycle_is_processable", None)

    assert predicate is not None
    assert predicate("chat_received", cycle_status) is False
    assert predicate("nearby_friend_chat_requested", cycle_status) is False
    assert predicate("chat_send_result", cycle_status) is False
    assert predicate("observation_updated", cycle_status) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    ["chat_received", "nearby_friend_chat_requested", "chat_send_result"],
)
async def test_successful_hosted_chat_does_not_advance_decision_sequence(event_type: str) -> None:
    executed: list[object] = []

    class Session:
        async def execute(self, statement, parameters):
            executed.append((statement, parameters))

    repository = inbox_repository.InboxRepository(session_factory=SimpleNamespace)
    event = SimpleNamespace(
        event_type=event_type,
        cycle_id="cycle-1",
        event_sequence=2**62,
    )

    row = {
        "event_type": event_type,
        "next_event_sequence": 2,
        "event_sequence": 2**62,
    }
    await repository._complete_cycle_success(Session(), row, event)

    assert executed == []


@pytest.mark.asyncio
async def test_hosted_chat_activity_write_preserves_decision_event_order_watermark() -> None:
    executed: list[tuple[object, dict[str, object]]] = []

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return "cycle-1"

    class Session:
        async def execute(self, statement, parameters):
            executed.append((statement, parameters))
            return Result()

    repository = ActivityPlanRepository(session_factory=SimpleNamespace)
    event = SimpleNamespace(
        cycle_id="cycle-1",
        row_id="chat-row-1",
        event_sequence=2**62,
    )

    await repository._write_hosted_chat_state(
        Session(),
        event,
        create_plaza_social_plan("plan-1"),
    )

    assert len(executed) == 1
    statement, parameters = executed[0]
    sql = str(statement)
    assert "activity_last_event_id" not in sql
    assert "activity_last_event_sequence" not in sql
    assert "activity_last_event_id" not in parameters
    assert "activity_last_event_sequence" not in parameters
    assert "last_event_sequence" not in parameters


@pytest.mark.asyncio
async def test_resolve_hosted_role_id_reads_current_session_snapshot_role_id() -> None:
    session = _ScalarSession("1248993658045202501")
    repository = inbox_repository.InboxRepository(session_factory=lambda: session)

    role_id = await repository.resolve_role_id("gateway-1", "session-1")

    assert role_id == "1248993658045202501"
    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert "latest_decision_context" in str(statement)
    assert "'session'" in str(statement)
    assert "'RoleId'" in str(statement)
    assert parameters == {"gateway_id": "gateway-1", "session_id": "session-1"}


@pytest.mark.asyncio
async def test_resolve_hosted_role_id_returns_none_when_snapshot_has_no_role_id() -> None:
    session = _ScalarSession(None)
    repository = inbox_repository.InboxRepository(session_factory=lambda: session)

    assert await repository.resolve_role_id("gateway-1", "session-1") is None
