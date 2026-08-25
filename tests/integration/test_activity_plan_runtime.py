from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.integration.llm_gateway_v2.activity_plan import ActivityPlanProposal
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanRepository,
)
from src.core.integration.llm_gateway_v2.activity_planner import ActivityPlanCoordinator
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2DecisionPlanner,
    GatewayV2DecisionService,
)
from src.core.integration.llm_gateway_v2.event_service import GatewayV2EventDispatcher
from src.core.integration.llm_gateway_v2.event_worker import EventProcessResult
from src.core.integration.llm_gateway_v2.inbox_repository import InboxRepository
from src.core.integration.llm_gateway_v2.outbox_repository import OutboxRepository
from src.core.integration.llm_gateway_v2.terminal_repository import TerminalRepository

pytestmark = pytest.mark.asyncio

TENANT_ID = UUID("00000000-0000-0000-0000-000000000099")
GATEWAY_ID = "activity-plan-test-gateway"
SESSION_ID = "activity-plan-session"
SKILLS = (
    "scene_tornado",
    "dance_auto_schedule",
    "hot_air_balloon_auto_schedule",
    "coffee_auto_schedule",
    "darts_auto_schedule",
    "paper_plane_auto_schedule",
)


@pytest.fixture(scope="module", autouse=True)
def _upgrade_schema(migration_config) -> None:
    command.upgrade(migration_config, "head")


@pytest.fixture
async def session_factory(verified_test_postgres_url: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(verified_test_postgres_url, poolclass=sa.pool.NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with engine.begin() as connection:
        for table in (
            "llm_gateway_skill_calls",
            "llm_gateway_decisions",
            "llm_gateway_events",
            "llm_gateway_control_cycles",
            "llm_gateway_sessions",
        ):
            await connection.execute(sa.text(f"DELETE FROM {table}"))
        await connection.execute(
            sa.text("DELETE FROM action_tracking WHERE tenant_id=:tenant_id"),
            {"tenant_id": TENANT_ID},
        )
        await connection.execute(sa.text("DELETE FROM tenants WHERE id=:tenant_id"), {"tenant_id": TENANT_ID})
        await connection.execute(
            sa.text(
                "INSERT INTO tenants (id, user_id, api_key, is_active, is_admin) "
                "VALUES (:id, :user_id, :api_key, true, false)"
            ),
            {"id": TENANT_ID, "user_id": "activity-plan-test-user", "api_key": "activity-plan-test-key"},
        )
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            for table in (
                "llm_gateway_skill_calls",
                "llm_gateway_decisions",
                "llm_gateway_events",
                "llm_gateway_control_cycles",
                "llm_gateway_sessions",
            ):
                await connection.execute(sa.text(f"DELETE FROM {table}"))
            await connection.execute(
                sa.text("DELETE FROM action_tracking WHERE tenant_id=:tenant_id"),
                {"tenant_id": TENANT_ID},
            )
            await connection.execute(sa.text("DELETE FROM tenants WHERE id=:tenant_id"), {"tenant_id": TENANT_ID})
        await engine.dispose()


def _available_skills() -> list[dict[str, object]]:
    return [
        {
            "SkillName": skill,
            "SchemaVersion": "v1",
            "RequireRunning": True,
            "CooldownMs": 0,
        }
        for skill in SKILLS
    ]


def _skill_hints() -> list[dict[str, object]]:
    hints: list[dict[str, object]] = []
    for skill in SKILLS:
        allowed_args: list[dict[str, object]] = []
        suggested_args: dict[str, object] = {}
        if skill == "dance_auto_schedule":
            allowed_args = [{"path": "score"}]
        elif skill == "darts_auto_schedule":
            allowed_args = [
                {"path": "score"},
                {"path": "darts"},
                {"path": "allowPurchaseWhenInsufficient"},
            ]
        elif skill == "paper_plane_auto_schedule":
            allowed_args = [
                {"path": "planeName"},
                {"path": "useTimeMs"},
                {"path": "isComplete"},
            ]
        elif skill == "coffee_auto_schedule":
            allowed_args = [{"path": "coffeeName"}]
            suggested_args = {"coffeeName": "latte"}
        hints.append(
            {
                "skillName": skill,
                "schemaVersion": "v1",
                "argumentStatus": "ready",
                "suggestedArgs": suggested_args,
                "allowedArgs": allowed_args,
                "missingArgs": [{"path": str(field["path"])} for field in allowed_args],
                "warnings": [],
                "nextSteps": [],
            }
        )
    return hints


def _lease(sequence: int, lease_id: str) -> dict[str, object]:
    return {
        "sessionId": SESSION_ID,
        "controlGeneration": 1,
        "decisionLeaseId": lease_id,
        "stateVersion": sequence,
        "leaseKind": "observation",
        "allowedActions": ["call_skill", "wait", "no_op"],
        "allowedSkillName": None,
        "allowedSkillNames": list(SKILLS),
        "parentSkillName": None,
    }


def _decision_context(*, lobby: bool, terminal: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "session": {
            "AccountId": "activity-account",
            "SessionId": SESSION_ID,
            "RoleId": "activity-role",
            "SceneId": 1 if lobby else 2,
            "SceneName": "Lobby" if lobby else "Plaza",
            "NavigationAvailable": not lobby,
            "SkillExecuting": False,
            "State": "Running",
        },
        "availableSkills": _available_skills(),
        "skillArgumentHints": _skill_hints(),
        "lastSkillResult": terminal,
    }


def _event(
    event_type: str,
    sequence: int,
    *,
    decision_id: str | None = None,
    skill_name: str | None = None,
    skill_call_id: str | None = None,
    lease: bool = True,
    terminal_status: str = "success",
    terminal_reason: str = "completed",
    terminal_failure_category: str | None = None,
    terminal_retryable: bool = False,
) -> object:
    occurred_at_ms = 1_800_000_000_000 + sequence
    lease_id = f"lease-{sequence}"
    top_lease_id = (
        lease_id if lease and event_type in {"session_started", "observation_updated", "skill_finished"} else None
    )
    if event_type in {"session_started", "observation_updated"}:
        payload: dict[str, object] = {
            "reason": "decision_requested",
            "lease": _lease(sequence, lease_id),
            "decisionContext": _decision_context(lobby=sequence == 1),
        }
    elif event_type == "skill_started":
        assert decision_id is not None and skill_name is not None and skill_call_id is not None
        payload = {
            "decisionId": decision_id,
            "skillName": skill_name,
            "skillCallId": skill_call_id,
            "startedAtMs": occurred_at_ms,
        }
    elif event_type == "skill_finished":
        assert decision_id is not None and skill_name is not None and skill_call_id is not None
        payload = {
            "decisionId": decision_id,
            "skillName": skill_name,
            "skillCallId": skill_call_id,
            "status": terminal_status,
            "reason": terminal_reason,
            "failureCategory": terminal_failure_category,
            "retryable": terminal_retryable,
            "startedAtMs": occurred_at_ms - 1,
            "finishedAtMs": occurred_at_ms,
        }
        if lease:
            payload["lease"] = _lease(sequence, lease_id)
            payload["decisionContext"] = _decision_context(
                lobby=False,
                terminal={
                    "status": terminal_status,
                    "reason": terminal_reason,
                    "retryable": terminal_retryable,
                    "skillName": skill_name,
                    "skillCallId": skill_call_id,
                },
            )
    elif event_type == "nearby_friend_chat_requested":
        payload = {
            "sessionId": SESSION_ID,
            "target": {"avatarId": "100", "roleId": "200"},
            "chatType": "friend",
            "distance": 2.0,
            "friendChatCount": 0,
            "conversation": {
                "conversationId": "activity-conversation-1",
                "pairKey": "100:200",
                "speakerRoleId": 100,
                "targetRoleId": 200,
                "brainUsername": "activity-role",
                "historyRounds": [],
                "completedRounds": 0,
                "maxRounds": 6,
                "expiresAtMs": 1_800_000_100_000,
            },
        }
    elif event_type == "chat_send_result":
        payload = {
            "sessionId": SESSION_ID,
            "chatMessageId": "activity-message-1",
            "target": {"avatarId": "100", "roleId": "200"},
            "chatType": "friend",
            "status": "sent",
            "completedAtMs": occurred_at_ms,
        }
    else:
        raise AssertionError(event_type)
    wire_state_version = 0 if event_type in {"nearby_friend_chat_requested", "chat_send_result"} else sequence
    event = {
        "eventId": f"event-{sequence}",
        "eventType": event_type,
        "sessionId": SESSION_ID,
        "stateVersion": wire_state_version,
        "decisionLeaseId": top_lease_id,
        "occurredAtMs": occurred_at_ms,
        "payload": payload,
    }
    if event_type not in {"nearby_friend_chat_requested", "chat_send_result"}:
        event.update(controlGeneration=1, eventSequence=sequence)
    return parse_gateway_v2_event(event)


def _first_proposal() -> ActivityPlanProposal:
    return ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_social",
            "goalSummary": "Enter the plaza and complete varied activities",
            "steps": [
                {
                    "stepId": "arrival",
                    "phase": "arrival",
                    "skillName": "scene_tornado",
                    "schemaVersion": "v1",
                    "intent": "enter plaza",
                },
                {
                    "stepId": "dance",
                    "phase": "activity",
                    "skillName": "dance_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "dance",
                },
                {
                    "stepId": "balloon",
                    "phase": "transport",
                    "skillName": "hot_air_balloon_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "ride balloon",
                },
                {"stepId": "social-opportunity", "phase": "social", "intent": "wait for a nearby friend"},
            ],
        }
    )


def _second_proposal() -> ActivityPlanProposal:
    return ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_variety",
            "goalSummary": "Continue with a fresh set of plaza activities",
            "steps": [
                {
                    "stepId": "coffee",
                    "phase": "activity",
                    "skillName": "coffee_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "have coffee",
                },
                {
                    "stepId": "darts",
                    "phase": "activity",
                    "skillName": "darts_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "play darts",
                },
                {
                    "stepId": "paper-plane",
                    "phase": "activity",
                    "skillName": "paper_plane_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "fly paper planes",
                },
            ],
        }
    )


@dataclass
class _PlanGenerator:
    # Lobby bootstrap uses the deterministic safe plan and intentionally does
    # not call the model. Start after that implicit bootstrap for replan tests.
    calls: int = 1

    async def generate(self, context: GatewayV2AgentContext, *, recent_actions, recent_failures):
        del context, recent_actions, recent_failures
        self.calls += 1
        return _first_proposal() if self.calls == 1 else _second_proposal()


class _FailingAgentRunner:
    async def ainvoke(self, state):
        del state
        raise AssertionError("the deterministic activity plan should avoid the decision model")


class _ChatStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_chat_received(self, gateway_id, event) -> None:
        del gateway_id, event
        self.calls.append("received")

    async def handle_nearby_friend_request(self, gateway_id, event) -> None:
        del gateway_id, event
        self.calls.append("nearby")

    async def handle_send_result(self, gateway_id, event) -> None:
        del gateway_id, event
        self.calls.append("result")


@dataclass
class _Runtime:
    inbox: InboxRepository
    outbox: OutboxRepository
    dispatcher: GatewayV2EventDispatcher
    chat: _ChatStub


def _runtime(factory, generator: _PlanGenerator, next_decision_id) -> _Runtime:
    inbox = InboxRepository(factory)
    outbox = OutboxRepository(factory, decision_id_factory=next_decision_id)
    activity = ActivityPlanRepository(
        factory,
        plan_id_factory=lambda: f"plan-generated-{generator.calls + 1}",
    )
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=_FailingAgentRunner()),
        repository=outbox,
        activity_coordinator=ActivityPlanCoordinator(repository=activity, generator=generator),
    )
    chat = _ChatStub()
    dispatcher = GatewayV2EventDispatcher(
        context_repository=inbox,
        terminal_repository=TerminalRepository(factory),
        outbox_repository=outbox,
        decision_planner=planner,
        hosted_chat_service=chat,
        activity_repository=activity,
    )
    return _Runtime(inbox, outbox, dispatcher, chat)


async def _dispatch(runtime: _Runtime, event: object) -> None:
    identity = InboundGatewayIdentity("activity-events", GATEWAY_ID, TENANT_ID)
    await runtime.inbox.accept_event_batch(identity, f"trace-{uuid4().hex}", (event,))
    claimed = await runtime.inbox.claim_next_event(
        worker_id="activity-worker",
        claim_ttl_ms=30_000,
        max_attempts=3,
    )
    assert claimed is not None
    result = await runtime.dispatcher(claimed)
    assert result == EventProcessResult("succeeded")
    assert await runtime.inbox.complete_event(
        claimed,
        result,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )


async def _decisions(factory) -> list[dict[str, object]]:
    async with factory() as session:
        result = await session.execute(
            sa.text(
                "SELECT decision_id, action, request_body_json, activity_plan_id, "
                "activity_plan_version, activity_step_id, activity_phase "
                "FROM llm_gateway_decisions ORDER BY created_at, id"
            )
        )
        return [dict(row) for row in result.mappings().all()]


async def test_activity_plan_survives_runtime_restart_and_advances_through_social_opportunity(
    session_factory,
) -> None:
    generator = _PlanGenerator()
    decision_number = 0

    def next_decision_id() -> str:
        nonlocal decision_number
        decision_number += 1
        return f"activity-decision-{decision_number}"

    runtime = _runtime(session_factory, generator, next_decision_id)
    await _dispatch(runtime, _event("session_started", 1))
    decisions = await _decisions(session_factory)
    assert decisions[-1]["action"] == "call_skill"
    assert decisions[-1]["activity_step_id"] == "arrival"

    await _dispatch(
        runtime,
        _event(
            "skill_started",
            2,
            decision_id=str(decisions[-1]["decision_id"]),
            skill_name="scene_tornado",
            skill_call_id="activity-call-1",
            lease=False,
        ),
    )
    await _dispatch(
        runtime,
        _event(
            "skill_finished",
            3,
            decision_id=str(decisions[-1]["decision_id"]),
            skill_name="scene_tornado",
            skill_call_id="activity-call-1",
        ),
    )
    decisions = await _decisions(session_factory)
    assert decisions[-1]["activity_step_id"] == "dance"

    runtime = _runtime(session_factory, generator, next_decision_id)
    await _dispatch(
        runtime,
        _event(
            "skill_started",
            4,
            decision_id=str(decisions[-1]["decision_id"]),
            skill_name="dance_auto_schedule",
            skill_call_id="activity-call-2",
            lease=False,
        ),
    )
    await _dispatch(
        runtime,
        _event(
            "skill_finished",
            5,
            decision_id=str(decisions[-1]["decision_id"]),
            skill_name="dance_auto_schedule",
            skill_call_id="activity-call-2",
        ),
    )
    decisions = await _decisions(session_factory)
    assert decisions[-1]["activity_step_id"] == "balloon"

    await _dispatch(
        runtime,
        _event(
            "skill_started",
            6,
            decision_id=str(decisions[-1]["decision_id"]),
            skill_name="hot_air_balloon_auto_schedule",
            skill_call_id="activity-call-3",
            lease=False,
        ),
    )
    await _dispatch(
        runtime,
        _event(
            "skill_finished",
            7,
            decision_id=str(decisions[-1]["decision_id"]),
            skill_name="hot_air_balloon_auto_schedule",
            skill_call_id="activity-call-3",
        ),
    )
    decisions = await _decisions(session_factory)
    assert decisions[-1]["activity_step_id"] == "coffee"
    assert decisions[-1]["action"] == "call_skill"

    await _dispatch(runtime, _event("nearby_friend_chat_requested", 8, lease=False))
    await _dispatch(runtime, _event("chat_send_result", 9, lease=False))
    decisions = await _decisions(session_factory)

    assert runtime.chat.calls == ["nearby", "result"]
    assert generator.calls == 2
    assert len(decisions) == 4
    assert [row["activity_step_id"] for row in decisions] == [
        "arrival",
        "dance",
        "balloon",
        "coffee",
    ]
    assert all(row["action"] != "wait" for row in decisions)
    assert sum(row["request_body_json"].get("skillName") == "scene_tornado" for row in decisions) == 1
    assert all(not any(key.startswith("activity") for key in row["request_body_json"]) for row in decisions)
    async with session_factory() as session:
        cycle = (
            (
                await session.execute(
                    sa.text(
                        "SELECT activity_plan_version, activity_status, activity_phase, "
                        "activity_current_step_id, activity_last_event_sequence "
                        "FROM llm_gateway_control_cycles"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert dict(cycle) == {
        "activity_plan_version": 2,
        "activity_status": "active",
        "activity_phase": "activity",
        "activity_current_step_id": "coffee",
        "activity_last_event_sequence": 7,
    }


async def test_non_retryable_parameter_failure_uses_new_lease_for_corrected_decision(
    session_factory,
) -> None:
    generator = _PlanGenerator()
    decision_number = 0

    def next_decision_id() -> str:
        nonlocal decision_number
        decision_number += 1
        return f"correction-decision-{decision_number}"

    runtime = _runtime(session_factory, generator, next_decision_id)
    await _dispatch(runtime, _event("session_started", 1))
    decisions = await _decisions(session_factory)
    arrival = decisions[-1]
    await _dispatch(
        runtime,
        _event(
            "skill_started",
            2,
            decision_id=str(arrival["decision_id"]),
            skill_name="scene_tornado",
            skill_call_id="correction-arrival-call",
            lease=False,
        ),
    )
    await _dispatch(
        runtime,
        _event(
            "skill_finished",
            3,
            decision_id=str(arrival["decision_id"]),
            skill_name="scene_tornado",
            skill_call_id="correction-arrival-call",
        ),
    )
    decisions = await _decisions(session_factory)
    first_dance = decisions[-1]
    assert first_dance["activity_step_id"] == "dance"
    assert first_dance["request_body_json"]["skillName"] == "dance_auto_schedule"

    await _dispatch(
        runtime,
        _event(
            "skill_started",
            4,
            decision_id=str(first_dance["decision_id"]),
            skill_name="dance_auto_schedule",
            skill_call_id="correction-dance-call-1",
            lease=False,
        ),
    )
    await _dispatch(
        runtime,
        _event(
            "skill_finished",
            5,
            decision_id=str(first_dance["decision_id"]),
            skill_name="dance_auto_schedule",
            skill_call_id="correction-dance-call-1",
            terminal_status="failed",
            terminal_reason="dance_score_invalid",
            terminal_failure_category="business_rejected",
            terminal_retryable=False,
        ),
    )
    decisions = await _decisions(session_factory)
    corrected_dance = decisions[-1]
    assert corrected_dance["decision_id"] != first_dance["decision_id"]
    assert corrected_dance["activity_step_id"] == "dance"
    assert corrected_dance["request_body_json"]["skillName"] == "dance_auto_schedule"
    assert 70 <= corrected_dance["request_body_json"]["arguments"]["score"] <= 120

    await _dispatch(
        runtime,
        _event(
            "skill_started",
            6,
            decision_id=str(corrected_dance["decision_id"]),
            skill_name="dance_auto_schedule",
            skill_call_id="correction-dance-call-2",
            lease=False,
        ),
    )
    await _dispatch(
        runtime,
        _event(
            "skill_finished",
            7,
            decision_id=str(corrected_dance["decision_id"]),
            skill_name="dance_auto_schedule",
            skill_call_id="correction-dance-call-2",
        ),
    )

    async with session_factory() as session:
        cycle = (
            (
                await session.execute(
                    sa.text(
                        "SELECT activity_current_step_id, activity_phase, activity_plan FROM llm_gateway_control_cycles"
                    )
                )
            )
            .mappings()
            .one()
        )
        terminals = (
            (
                await session.execute(
                    sa.text(
                        "SELECT status, reason, retryable FROM llm_gateway_skill_calls "
                        "WHERE skill_name='dance_auto_schedule' ORDER BY created_at"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert cycle["activity_current_step_id"] == "balloon"
    assert cycle["activity_phase"] == "transport"
    assert [row["status"] for row in terminals] == ["failed", "succeeded"]
    assert terminals[0]["reason"] == "dance_score_invalid"
    assert terminals[0]["retryable"] is False
