from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.integration.llm_gateway_v2.activity_plan import ActivityPlanProposal
from src.core.integration.llm_gateway_v2.activity_plan_repository import ActivityPlanRepository
from src.core.integration.llm_gateway_v2.activity_planner import ActivityPlanCoordinator
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import GatewayV2Event, parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2DecisionPlanner,
    GatewayV2DecisionService,
    gateway_v2_activity_skill_is_permitted,
)
from src.core.integration.llm_gateway_v2.event_service import GatewayV2EventDispatcher
from src.core.integration.llm_gateway_v2.event_worker import EventProcessResult
from src.core.integration.llm_gateway_v2.inbox_repository import InboxRepository
from src.core.integration.llm_gateway_v2.outbox_repository import OutboxRepository
from src.core.integration.llm_gateway_v2.scene_catalog import SceneCatalog, load_default_scene_catalog
from src.core.integration.llm_gateway_v2.terminal_repository import TerminalRepository

pytestmark = pytest.mark.asyncio

TENANT_ID = UUID("00000000-0000-0000-0000-000000000130")
GATEWAY_ID = "activity-30m-simulation-gateway"
ROLE_COUNT = 10
SIMULATION_DURATION_MS = 30 * 60 * 1_000
SIMULATION_START_MS = 1_800_000_000_000
PLAZA_SCENE_ID = 7
PLAZA_SCENE_NAME = "CJ_guangchang"

SKILLS = (
    "scene_tornado",
    "dance_auto_schedule",
    "hot_air_balloon_auto_schedule",
    "coffee_auto_schedule",
    "darts_auto_schedule",
    "shooting_auto_schedule",
    "paper_plane_auto_schedule",
    "draw_lots_auto_schedule",
    "wish_board_auto_schedule",
    "helicopter_auto_schedule",
    "elevator_auto_schedule",
    "seat_sit",
    "sign_in",
    "play_action",
    "jump",
    "observe_state",
    "move_to",
)


@pytest.fixture(scope="module", autouse=True)
def _upgrade_schema(migration_config) -> None:
    command.upgrade(migration_config, "head")


@pytest.fixture
async def session_factory(
    verified_test_postgres_url: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(verified_test_postgres_url)
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
        await connection.execute(
            sa.text("DELETE FROM tenants WHERE id=:tenant_id"),
            {"tenant_id": TENANT_ID},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO tenants (id, user_id, api_key, is_active, is_admin) "
                "VALUES (:id, :user_id, :api_key, true, false)"
            ),
            {
                "id": TENANT_ID,
                "user_id": "activity-30m-simulation-user",
                "api_key": "activity-30m-simulation-key",
            },
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
            await connection.execute(
                sa.text("DELETE FROM tenants WHERE id=:tenant_id"),
                {"tenant_id": TENANT_ID},
            )
        await engine.dispose()


@dataclass
class SimulatedRole:
    index: int
    session_id: str
    account_id: str
    role_id: str
    event_sequence: int = 0
    virtual_time_ms: int = SIMULATION_START_MS
    scene_id: int = 1
    scene_name: str = "Lobby"
    navigation_available: bool = False
    last_skill_name: str | None = None
    position: dict[str, float] | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def next_sequence(self) -> int:
        self.event_sequence += 1
        return self.event_sequence


class RepeatedAiPlanGenerator:
    async def generate(
        self,
        context: GatewayV2AgentContext,
        *,
        recent_actions: tuple[Mapping[str, Any], ...],
        recent_failures: tuple[Mapping[str, Any], ...],
    ) -> ActivityPlanProposal:
        del recent_actions, recent_failures
        lobby = str(context.session_snapshot.get("SceneId")) == "1"
        if lobby:
            steps = [
                _proposal_step("arrival", "arrival", "scene_tornado", "Enter the plaza"),
                _proposal_step("dance", "activity", "dance_auto_schedule", "Dance in the plaza"),
                _proposal_step(
                    "balloon",
                    "transport",
                    "hot_air_balloon_auto_schedule",
                    "Ride the hot air balloon",
                ),
            ]
            goal_id = "enter_plaza"
            summary = "Enter the plaza and start a varied activity sequence"
        else:
            steps = [
                _proposal_step("dance", "activity", "dance_auto_schedule", "Dance in the current scene"),
                _proposal_step(
                    "balloon",
                    "transport",
                    "hot_air_balloon_auto_schedule",
                    "Ride the hot air balloon",
                ),
                _proposal_step("coffee", "activity", "coffee_auto_schedule", "Have coffee"),
            ]
            goal_id = "continue_varied_activity"
            summary = "Continue varied activities and movement without waiting"
        steps.append(
            {
                "stepId": "social-opportunity",
                "phase": "social",
                "intent": "Remain open to a nearby friend chat opportunity",
            }
        )
        return ActivityPlanProposal.model_validate({"goalId": goal_id, "goalSummary": summary, "steps": steps})


def _proposal_step(step_id: str, phase: str, skill_name: str, intent: str) -> dict[str, str]:
    return {
        "stepId": step_id,
        "phase": phase,
        "skillName": skill_name,
        "schemaVersion": "v1",
        "intent": intent,
    }


class FailingAgentRunner:
    async def ainvoke(self, state: dict[str, Any]) -> Mapping[str, Any]:
        del state
        raise AssertionError("activity-plan simulation unexpectedly called the action model")


@dataclass(frozen=True)
class SimulationRuntime:
    inbox: InboxRepository
    dispatcher: GatewayV2EventDispatcher


def _runtime(
    factory: async_sessionmaker[AsyncSession],
    scene_catalog: SceneCatalog,
) -> SimulationRuntime:
    inbox = InboxRepository(factory)
    outbox = OutboxRepository(factory)
    activity = ActivityPlanRepository(factory)
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=FailingAgentRunner()),
        repository=outbox,
        activity_coordinator=ActivityPlanCoordinator(
            repository=activity,
            generator=RepeatedAiPlanGenerator(),
            step_authorizer=gateway_v2_activity_skill_is_permitted,
            scene_catalog=scene_catalog,
        ),
        scene_catalog=scene_catalog,
    )
    dispatcher = GatewayV2EventDispatcher(
        context_repository=inbox,
        terminal_repository=TerminalRepository(factory),
        outbox_repository=outbox,
        decision_planner=planner,
        activity_repository=activity,
    )
    return SimulationRuntime(inbox=inbox, dispatcher=dispatcher)


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
        move_paths = (
            [{"path": f"target.{axis}", "type": "number"} for axis in ("x", "y", "z")] if skill == "move_to" else []
        )
        hints.append(
            {
                "skillName": skill,
                "schemaVersion": "v1",
                "argumentStatus": "missing" if move_paths else "ready",
                "suggestedArgs": {},
                "allowedArgs": move_paths,
                "missingArgs": move_paths,
                "warnings": [],
                "nextSteps": [],
            }
        )
    return hints


def _lease(role: SimulatedRole, sequence: int) -> dict[str, object]:
    return {
        "sessionId": role.session_id,
        "controlGeneration": 1,
        "decisionLeaseId": f"lease-{role.index}-{sequence}",
        "stateVersion": sequence,
        "leaseKind": "observation",
        "allowedActions": ["call_skill", "wait", "no_op"],
        "allowedSkillName": None,
        "allowedSkillNames": list(SKILLS),
        "parentSkillName": None,
    }


def _decision_context(
    role: SimulatedRole,
    *,
    terminal: dict[str, object] | None = None,
) -> dict[str, object]:
    session: dict[str, object] = {
        "AccountId": role.account_id,
        "SessionId": role.session_id,
        "RoleId": role.role_id,
        "SceneId": role.scene_id,
        "SceneName": role.scene_name,
        "NavigationAvailable": role.navigation_available,
        "SkillExecuting": False,
        "LastSkillName": role.last_skill_name,
        "State": "Running",
    }
    if role.position is not None:
        session["Position"] = role.position
    return {
        "session": session,
        "availableSkills": _available_skills(),
        "skillArgumentHints": _skill_hints(),
        "lastSkillResult": terminal,
    }


def _session_started(role: SimulatedRole) -> GatewayV2Event:
    sequence = role.next_sequence()
    lease = _lease(role, sequence)
    return parse_gateway_v2_event(
        {
            "eventId": f"sim-{role.index}-{sequence}",
            "eventType": "session_started",
            "sessionId": role.session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": sequence,
            "decisionLeaseId": lease["decisionLeaseId"],
            "occurredAtMs": role.virtual_time_ms,
            "payload": {
                "reason": "simulation_started",
                "lease": lease,
                "decisionContext": _decision_context(role),
            },
        }
    )


def _skill_started(
    role: SimulatedRole,
    decision: Mapping[str, Any],
    skill_call_id: str,
) -> GatewayV2Event:
    sequence = role.next_sequence()
    return parse_gateway_v2_event(
        {
            "eventId": f"sim-{role.index}-{sequence}",
            "eventType": "skill_started",
            "sessionId": role.session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": sequence,
            "decisionLeaseId": None,
            "occurredAtMs": role.virtual_time_ms + 100,
            "payload": {
                "decisionId": decision["decisionId"],
                "skillName": decision["skillName"],
                "skillCallId": skill_call_id,
                "startedAtMs": role.virtual_time_ms + 100,
            },
        }
    )


def _skill_finished(
    role: SimulatedRole,
    decision: Mapping[str, Any],
    skill_call_id: str,
    duration_ms: int,
) -> GatewayV2Event:
    role.virtual_time_ms += duration_ms
    role.last_skill_name = str(decision["skillName"])
    if role.last_skill_name == "scene_tornado":
        role.scene_id = PLAZA_SCENE_ID
        role.scene_name = PLAZA_SCENE_NAME
        role.navigation_available = True
    if role.last_skill_name == "move_to":
        arguments = decision.get("arguments")
        target = arguments.get("target") if isinstance(arguments, Mapping) else None
        if isinstance(target, Mapping):
            role.position = {axis: float(target[axis]) for axis in ("x", "y", "z")}

    sequence = role.next_sequence()
    lease = _lease(role, sequence)
    terminal = {
        "status": "success",
        "skillName": role.last_skill_name,
        "skillCallId": skill_call_id,
    }
    return parse_gateway_v2_event(
        {
            "eventId": f"sim-{role.index}-{sequence}",
            "eventType": "skill_finished",
            "sessionId": role.session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": sequence,
            "decisionLeaseId": lease["decisionLeaseId"],
            "occurredAtMs": role.virtual_time_ms,
            "payload": {
                "decisionId": decision["decisionId"],
                "skillName": role.last_skill_name,
                "skillCallId": skill_call_id,
                "status": "success",
                "reason": "simulation_completed",
                "failureCategory": None,
                "retryable": False,
                "startedAtMs": role.virtual_time_ms - duration_ms + 100,
                "finishedAtMs": role.virtual_time_ms,
                "lease": lease,
                "decisionContext": _decision_context(role, terminal=terminal),
            },
        }
    )


def _observation_updated(role: SimulatedRole, delay_ms: int) -> GatewayV2Event:
    role.virtual_time_ms += delay_ms
    sequence = role.next_sequence()
    lease = _lease(role, sequence)
    return parse_gateway_v2_event(
        {
            "eventId": f"sim-{role.index}-{sequence}",
            "eventType": "observation_updated",
            "sessionId": role.session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": sequence,
            "decisionLeaseId": lease["decisionLeaseId"],
            "occurredAtMs": role.virtual_time_ms,
            "payload": {
                "reason": "simulation_timer",
                "lease": lease,
                "decisionContext": _decision_context(role),
            },
        }
    )


async def _dispatch(
    runtime: SimulationRuntime,
    event: GatewayV2Event,
) -> None:
    identity = InboundGatewayIdentity("activity-30m-events", GATEWAY_ID, TENANT_ID)
    await runtime.inbox.accept_event_batch(identity, f"trace-{uuid4().hex}", (event,))
    claimed = await runtime.inbox.claim_next_event(
        worker_id="activity-30m-worker",
        claim_ttl_ms=30_000,
        max_attempts=3,
    )
    assert claimed is not None
    assert claimed.event_id == event.event_id
    result = await runtime.dispatcher(claimed)
    assert result == EventProcessResult("succeeded")
    assert await runtime.inbox.complete_event(
        claimed,
        result,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )


async def _latest_decision(
    factory: async_sessionmaker[AsyncSession],
    role: SimulatedRole,
) -> dict[str, Any]:
    async with factory() as session:
        result = await session.execute(
            sa.text(
                "SELECT request_body_json FROM llm_gateway_decisions "
                "WHERE session_id=:session_id ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"session_id": role.session_id},
        )
        body = result.scalar_one()
    assert isinstance(body, dict)
    return body


def _skill_duration_ms(skill_name: str) -> int:
    if skill_name == "scene_tornado":
        return 15_000
    if skill_name == "move_to":
        return 45_000
    if skill_name in {"hot_air_balloon_auto_schedule", "helicopter_auto_schedule"}:
        return 120_000
    return 60_000


def _max_consecutive_skill_count(decisions: list[dict[str, Any]]) -> int:
    maximum = 0
    current = 0
    previous: str | None = None
    for decision in decisions:
        skill = decision.get("skillName") if decision.get("action") == "call_skill" else None
        if not isinstance(skill, str):
            previous = None
            current = 0
            continue
        current = current + 1 if skill == previous else 1
        previous = skill
        maximum = max(maximum, current)
    return maximum


def _first_repeated_skill_context(decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    skills = [
        str(decision["skillName"])
        for decision in decisions
        if decision["action"] == "call_skill"
    ]
    for index in range(1, len(skills)):
        if skills[index] == skills[index - 1]:
            return {
                "skill": skills[index],
                "index": index,
                "nearbySkills": skills[max(0, index - 3) : index + 4],
            }
    return None


async def _database_summary(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    async with factory() as session:
        event_rows = (
            (await session.execute(sa.text("SELECT status, count(*) AS count FROM llm_gateway_events GROUP BY status")))
            .mappings()
            .all()
        )
        cycle_rows = (
            (
                await session.execute(
                    sa.text(
                        "SELECT session_id, activity_plan_version, activity_status, "
                        "activity_current_step_id FROM llm_gateway_control_cycles ORDER BY session_id"
                    )
                )
            )
            .mappings()
            .all()
        )
        decision_count = int(await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions")) or 0)
        skill_call_rows = (
            (
                await session.execute(
                    sa.text("SELECT status, count(*) AS count FROM llm_gateway_skill_calls GROUP BY status")
                )
            )
            .mappings()
            .all()
        )
    return {
        "events": {str(row["status"]): int(row["count"]) for row in event_rows},
        "decisions": decision_count,
        "skillCalls": {str(row["status"]): int(row["count"]) for row in skill_call_rows},
        "cycles": [dict(row) for row in cycle_rows],
    }


async def test_ten_roles_run_for_thirty_virtual_minutes_without_decision_degradation(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene_catalog = load_default_scene_catalog()
    runtime = _runtime(session_factory, scene_catalog)
    roles = [
        SimulatedRole(
            index=index,
            session_id=f"activity-30m-session-{index}",
            account_id=f"activity-30m-account-{index}",
            role_id=str(3_574_531_302_836_404_224 + index),
            virtual_time_ms=SIMULATION_START_MS + index,
        )
        for index in range(ROLE_COUNT)
    ]

    for role in roles:
        await _dispatch(runtime, _session_started(role))
        role.decisions.append(await _latest_decision(session_factory, role))

    while any(role.virtual_time_ms < SIMULATION_START_MS + SIMULATION_DURATION_MS for role in roles):
        role = min(
            (
                candidate
                for candidate in roles
                if candidate.virtual_time_ms < SIMULATION_START_MS + SIMULATION_DURATION_MS
            ),
            key=lambda candidate: (candidate.virtual_time_ms, candidate.index),
        )
        decision = role.decisions[-1]
        action = decision["action"]
        if action == "call_skill":
            skill_name = str(decision["skillName"])
            skill_call_id = f"sim-call-{role.index}-{len(role.decisions)}"
            await _dispatch(runtime, _skill_started(role, decision, skill_call_id))
            await _dispatch(
                runtime,
                _skill_finished(
                    role,
                    decision,
                    skill_call_id,
                    _skill_duration_ms(skill_name),
                ),
            )
        elif action == "wait":
            wait_ms = decision.get("waitMs")
            assert isinstance(wait_ms, int) and not isinstance(wait_ms, bool) and wait_ms > 0
            await _dispatch(runtime, _observation_updated(role, wait_ms))
        elif action == "no_op":
            await _dispatch(runtime, _observation_updated(role, 10_000))
        else:
            pytest.fail(f"unexpected stop_hosting decision for {role.session_id}")
        role.decisions.append(await _latest_decision(session_factory, role))

    expected_plaza_coordinates = {
        target.coordinates.comparison_key() for target in scene_catalog.targets_for_scene(PLAZA_SCENE_ID)
    }
    action_counts: Counter[str] = Counter()
    skill_counts: Counter[str] = Counter()
    first_post_lobby_skills: list[str] = []
    per_role: list[dict[str, Any]] = []
    all_move_coordinates: list[tuple[float, float, float]] = []

    for role in roles:
        actions = [str(decision["action"]) for decision in role.decisions]
        action_counts.update(actions)
        skills = [str(decision["skillName"]) for decision in role.decisions if decision["action"] == "call_skill"]
        skill_counts.update(skills)
        assert skills[0] == "scene_tornado"
        assert len(skills) > 1
        first_post_lobby_skills.append(skills[1])

        role_move_coordinates: list[tuple[float, float, float]] = []
        for decision in role.decisions:
            assert decision["contractVersion"] == "llm-gateway-http-v2"
            assert decision["sessionId"] == role.session_id
            assert decision["controlGeneration"] == 1
            if decision["action"] == "wait":
                assert isinstance(decision.get("waitMs"), int)
            else:
                assert "waitMs" not in decision
            if decision.get("skillName") != "move_to":
                continue
            target = decision["arguments"]["target"]
            coordinates = (
                round(float(target["x"]), 6),
                round(float(target["y"]), 6),
                round(float(target["z"]), 6),
            )
            assert coordinates in expected_plaza_coordinates
            role_move_coordinates.append(coordinates)
            all_move_coordinates.append(coordinates)

        assert role.virtual_time_ms >= SIMULATION_START_MS + SIMULATION_DURATION_MS
        assert len(set(skills)) >= 7
        assert _max_consecutive_skill_count(role.decisions) == 1, {
            "roleId": role.role_id,
            "repeat": _first_repeated_skill_context(role.decisions),
        }
        assert role_move_coordinates
        assert len(role_move_coordinates) == len(set(role_move_coordinates))
        per_role.append(
            {
                "roleId": role.role_id,
                "decisionCount": len(role.decisions),
                "uniqueSkills": len(set(skills)),
                "moveCount": len(role_move_coordinates),
                "maxSameSkillStreak": _max_consecutive_skill_count(role.decisions),
                "planTimeSeconds": round((role.virtual_time_ms - SIMULATION_START_MS) / 1_000, 3),
            }
        )

    database = await _database_summary(session_factory)
    assert len(set(first_post_lobby_skills)) == ROLE_COUNT
    assert action_counts["wait"] == 0
    assert action_counts["no_op"] == 0
    assert action_counts["stop_hosting"] == 0
    assert action_counts["call_skill"] == sum(len(role.decisions) for role in roles)
    assert len(all_move_coordinates) > ROLE_COUNT
    assert database["events"] == {"succeeded": sum(role.event_sequence for role in roles)}
    assert database["skillCalls"] == {"succeeded": action_counts["call_skill"] - ROLE_COUNT}
    assert database["decisions"] == action_counts["call_skill"]
    assert len(database["cycles"]) == ROLE_COUNT
    assert all(int(cycle["activity_plan_version"]) >= 4 for cycle in database["cycles"])
    assert all(cycle["activity_status"] == "active" for cycle in database["cycles"])

    report = {
        "virtualDurationMinutes": 30,
        "roles": ROLE_COUNT,
        "totalDecisions": database["decisions"],
        "actionCounts": dict(sorted(action_counts.items())),
        "skillCounts": dict(sorted(skill_counts.items())),
        "firstPostLobbySkills": first_post_lobby_skills,
        "uniqueMoveCoordinates": len(set(all_move_coordinates)),
        "eventStatuses": database["events"],
        "skillCallStatuses": database["skillCalls"],
        "planVersions": [int(cycle["activity_plan_version"]) for cycle in database["cycles"]],
        "perRole": per_role,
    }
    with capsys.disabled():
        print("\nACTIVITY_30M_SIMULATION=" + json.dumps(report, ensure_ascii=False, sort_keys=True))
