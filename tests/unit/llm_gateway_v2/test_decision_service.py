from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.core.agents.gateway_v2 import build_gateway_v2_decision_graph
from src.core.agents.gateway_v2_models import (
    GatewayV2CallSkillAction,
    GatewayV2NoOpAction,
    GatewayV2StopHostingAction,
    GatewayV2WaitAction,
)
from src.core.agents.gateway_v2_prompts import GATEWAY_V2_ACTION_REASONING_SYSTEM
from src.core.agents.models import RecommendedAction
from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2AgentExecutionError,
    GatewayV2DecisionPlanner,
    GatewayV2DecisionSelectionError,
    GatewayV2DecisionService,
    build_gateway_v2_agent_context,
    freeze_gateway_v2_decision,
    select_gateway_v2_action,
)
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent, EventProcessResult
from src.core.integration.llm_gateway_v2.outbox_repository import PlannedDecision


def _lease(
    *,
    lease_kind: str = "hosting_control",
    allowed_actions: list[str] | None = None,
    available_skills: list[dict[str, str]] | None = None,
    hints: list[dict[str, Any]] | None = None,
    allowed_skill_name: str | None = None,
    allowed_skill_names: list[str] | None = None,
    parent_skill_name: str | None = None,
) -> dict[str, Any]:
    skill_inputs = (
        available_skills
        if available_skills is not None
        else [{"skillName": "jump", "schemaVersion": "v1"}]
    )
    hint_inputs = (
        hints
        if hints is not None
        else [
            {
                "skillName": "jump",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            }
        ]
    )
    descriptors = [
        {
            "SkillName": skill["skillName"],
            "SchemaVersion": skill["schemaVersion"],
            "RequireRunning": True,
            "CooldownMs": 0,
        }
        for skill in skill_inputs
    ]
    argument_hints = [
        {
            "skillName": hint["skillName"],
            "schemaVersion": hint["schemaVersion"],
            "argumentStatus": "ready",
            "suggestedArgs": {},
            "allowedArgs": [
                {"path": path}
                for path in hint.get("allowedArgs", [])
            ],
            "missingArgs": [
                {"path": path}
                for path in hint.get("missingArgs", [])
            ],
            "warnings": [],
            "nextSteps": [],
        }
        for hint in hint_inputs
    ]
    allowed_names = (
        allowed_skill_names
        if allowed_skill_names is not None
        else [skill["skillName"] for skill in skill_inputs]
    )
    if allowed_skill_name is None and len(allowed_names) == 1:
        allowed_skill_name = allowed_names[0]
    return {
        "reason": "decision_requested",
        "lease": {
            "sessionId": "session-1",
            "controlGeneration": 3,
            "decisionLeaseId": "lease-1",
            "stateVersion": 7,
            "leaseKind": lease_kind,
            "allowedActions": allowed_actions or ["call_skill", "wait", "no_op"],
            "allowedSkillName": allowed_skill_name,
            "allowedSkillNames": allowed_names,
            "parentSkillName": parent_skill_name,
        },
        "decisionContext": {
            "session": {
                "status": "active",
                "accountId": "account-1",
                "position": {"x": 1, "y": 2, "z": 3},
            },
            "availableSkills": descriptors,
            "skillArgumentHints": argument_hints,
        },
    }


def _event(*, lease: dict[str, Any] | None = None, terminal: dict[str, Any] | None = None):
    decision_payload = lease or _lease()
    if terminal is None:
        return parse_gateway_v2_event(
            {
                "eventId": "event-2",
                "eventType": "observation_updated",
                "sessionId": "session-1",
                "controlGeneration": 3,
                "eventSequence": 2,
                "stateVersion": 7,
                "decisionLeaseId": "lease-1",
                "occurredAtMs": 1_700_000_000_002,
                "payload": decision_payload,
            }
        )
    status = terminal["status"]
    return parse_gateway_v2_event(
        {
            "eventId": "event-3",
            "eventType": "skill_finished",
            "sessionId": "session-1",
            "controlGeneration": 3,
            "eventSequence": 3,
            "stateVersion": 7,
            "decisionLeaseId": "lease-1",
            "occurredAtMs": 1_700_000_000_003,
            "payload": {
                "decisionId": "decision-1",
                "skillName": "jump",
                "skillCallId": "call-1",
                "status": status,
                "reason": terminal.get("reason", "ok"),
                "failureCategory": terminal.get("failureCategory"),
                "retryable": terminal.get("retryable", False),
                "startedAtMs": 1_700_000_000_002,
                "finishedAtMs": 1_700_000_000_003,
                "lease": decision_payload["lease"],
                "decisionContext": decision_payload["decisionContext"],
            },
        }
    )


def _claimed_for_planner() -> ClaimedGatewayEvent:
    event = _event()
    now = datetime.now(UTC)
    return ClaimedGatewayEvent(
        row_id=uuid4(),
        tenant_id=UUID("00000000-0000-0000-0000-000000000074"),
        cycle_id=uuid4(),
        gateway_id="gateway-1",
        session_id=event.session_id,
        event_id=event.event_id,
        event_type=event.event_type,
        control_generation=event.control_generation,
        event_sequence=event.event_sequence,
        event=event,
        content_hash="d" * 64,
        trace_id="trace-1",
        claim_token=uuid4(),
        claimed_fence_version=3,
        attempt_count=1,
        locked_by="worker-1",
        lock_until=now + timedelta(seconds=30),
    )


def _skill(
    name: str,
    *,
    schema: str = "v1",
    arguments: dict[str, Any] | None = None,
) -> GatewayV2CallSkillAction:
    return GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": name,
            "schemaVersion": schema,
            "arguments": arguments or {},
            "reason": "test decision",
        }
    )


def test_context_contains_only_gateway_lease_scope_and_terminal_result() -> None:
    context = build_gateway_v2_agent_context(
        _event(
            terminal={
                "status": "failed",
                "failureCategory": "business_rejected",
                "reason": "blocked",
                "retryable": False,
            }
        )
    )

    assert context.session_id == "session-1"
    assert context.control_generation == 3
    assert context.event_sequence == 3
    assert context.state_version == 7
    assert context.decision_lease_id == "lease-1"
    assert context.lease_kind == "hosting_control"
    assert context.allowed_decision_actions == ("call_skill", "wait", "no_op")
    assert context.allowed_skill_name == "jump"
    assert context.allowed_skill_names == ("jump",)
    assert context.parent_skill_name is None
    assert context.session_snapshot == {
        "status": "active",
        "accountId": "account-1",
        "position": {"x": 1, "y": 2, "z": 3},
    }
    assert context.terminal_result == {
        "status": "failed",
        "failureCategory": "business_rejected",
        "reason": "blocked",
        "retryable": False,
    }


def test_context_uses_verified_parent_and_skill_allowlist_from_lease() -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind="movement_control",
                allowed_skill_name="stop_move",
                allowed_skill_names=["stop_move"],
                parent_skill_name="move_to",
                available_skills=[
                    {"skillName": "jump", "schemaVersion": "v1"},
                    {"skillName": "stop_move", "schemaVersion": "v1"},
                ],
                hints=[
                    {
                        "skillName": "jump",
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    },
                    {
                        "skillName": "stop_move",
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    },
                ],
            )
        )
    )

    assert context.parent_skill_name == "move_to"
    assert context.allowed_skill_name == "stop_move"
    assert context.allowed_skill_names == ("stop_move",)
    selected = select_gateway_v2_action(
        context,
        [_skill("jump"), _skill("stop_move")],
    )
    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == "stop_move"


def test_selector_restricts_skill_name_and_schema_to_available_skills() -> None:
    context = build_gateway_v2_agent_context(_event())

    selected = select_gateway_v2_action(
        context,
        [_skill("ground"), _skill("jump", schema="v2"), _skill("jump")],
    )

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == "jump"
    assert selected.schema_version == "v1"


@pytest.mark.parametrize("parent_skill_name", [None, "jump", "hot_air_balloon_auto_schedule"])
def test_movement_stop_move_requires_move_to_parent(parent_skill_name: str | None) -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind="movement_control",
                allowed_skill_name="stop_move",
                allowed_skill_names=["stop_move"],
                parent_skill_name=parent_skill_name,
                available_skills=[{"skillName": "stop_move", "schemaVersion": "v1"}],
                hints=[
                    {
                        "skillName": "stop_move",
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    }
                ],
            )
        )
    )

    selected = select_gateway_v2_action(context, [_skill("stop_move")])

    assert isinstance(selected, GatewayV2WaitAction)


@pytest.mark.parametrize("lease_kind", ["vehicle_cancel_window", "vehicle_recovery"])
@pytest.mark.parametrize(
    ("parent_skill_name", "exit_skill_name"),
    [
        ("hot_air_balloon_auto_schedule", "hot_air_balloon_exit"),
        ("helicopter_auto_schedule", "helicopter_exit"),
    ],
)
def test_vehicle_lease_accepts_only_the_exit_paired_with_its_parent(
    lease_kind: str,
    parent_skill_name: str,
    exit_skill_name: str,
) -> None:
    other_exit = "helicopter_exit" if exit_skill_name == "hot_air_balloon_exit" else "hot_air_balloon_exit"
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind=lease_kind,
                allowed_skill_name=None,
                allowed_skill_names=[exit_skill_name, other_exit],
                parent_skill_name=parent_skill_name,
                available_skills=[
                    {"skillName": exit_skill_name, "schemaVersion": "v1"},
                    {"skillName": other_exit, "schemaVersion": "v1"},
                ],
                hints=[
                    {
                        "skillName": exit_skill_name,
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    },
                    {
                        "skillName": other_exit,
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    },
                ],
            )
        )
    )

    selected = select_gateway_v2_action(
        context,
        [_skill(other_exit), _skill(exit_skill_name)],
    )

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == exit_skill_name


@pytest.mark.parametrize("lease_kind", ["vehicle_cancel_window", "vehicle_recovery"])
def test_vehicle_lease_rejects_exit_when_parent_is_not_a_known_vehicle_auto_skill(
    lease_kind: str,
) -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind=lease_kind,
                allowed_skill_name="hot_air_balloon_exit",
                allowed_skill_names=["hot_air_balloon_exit"],
                parent_skill_name="move_to",
                available_skills=[{"skillName": "hot_air_balloon_exit", "schemaVersion": "v1"}],
                hints=[
                    {
                        "skillName": "hot_air_balloon_exit",
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    }
                ],
            )
        )
    )

    selected = select_gateway_v2_action(context, [_skill("hot_air_balloon_exit")])

    assert isinstance(selected, GatewayV2WaitAction)


def test_selector_rejects_argument_paths_outside_hint_allowlist() -> None:
    lease = _lease(
        available_skills=[{"skillName": "teleport", "schemaVersion": "v3"}],
        hints=[
            {
                "skillName": "teleport",
                "schemaVersion": "v3",
                "allowedArgs": ["target.x"],
                "missingArgs": ["target.x"],
            }
        ],
    )
    context = build_gateway_v2_agent_context(_event(lease=lease))

    selected = select_gateway_v2_action(
        context,
        [_skill("teleport", schema="v3", arguments={"target": {"x": 1, "y": 2}})],
    )

    assert isinstance(selected, GatewayV2WaitAction)


def test_selector_rejects_skill_when_required_argument_is_missing() -> None:
    lease = _lease(
        available_skills=[{"skillName": "teleport", "schemaVersion": "v3"}],
        hints=[
            {
                "skillName": "teleport",
                "schemaVersion": "v3",
                "allowedArgs": ["target.x", "target.y"],
                "missingArgs": ["target.y"],
            }
        ],
        allowed_actions=["call_skill", "no_op"],
    )
    context = build_gateway_v2_agent_context(_event(lease=lease))

    selected = select_gateway_v2_action(
        context,
        [_skill("teleport", schema="v3", arguments={"target": {"x": 1}})],
    )

    assert isinstance(selected, GatewayV2NoOpAction)


def test_selector_requires_call_skill_action_in_lease_scope() -> None:
    context = build_gateway_v2_agent_context(
        _event(lease=_lease(allowed_actions=["wait"], available_skills=[], hints=[]))
    )

    selected = select_gateway_v2_action(context, [_skill("jump")])

    assert isinstance(selected, GatewayV2WaitAction)


def test_movement_control_allows_only_jump_and_stop_move_skill_intersection() -> None:
    skills = [
        {"skillName": "ground", "schemaVersion": "v1"},
        {"skillName": "move_to", "schemaVersion": "v1"},
        {"skillName": "jump", "schemaVersion": "v1"},
    ]
    hints = [
        {"skillName": name, "schemaVersion": "v1", "allowedArgs": [], "missingArgs": []}
        for name in ("ground", "move_to", "jump")
    ]
    context = build_gateway_v2_agent_context(
        _event(lease=_lease(lease_kind="movement_control", available_skills=skills, hints=hints))
    )

    selected = select_gateway_v2_action(
        context,
        [
            _skill("ground"),
            _skill("move_to", arguments={"target": {"x": 1, "y": 2, "z": 3}}),
            _skill("jump"),
        ],
    )

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == "jump"


def test_ground_is_never_treated_as_a_published_llm_skill() -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                available_skills=[{"skillName": "ground", "schemaVersion": "v1"}],
                hints=[
                    {
                        "skillName": "ground",
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    }
                ],
                allowed_actions=["call_skill", "no_op"],
            )
        )
    )

    selected = select_gateway_v2_action(context, [_skill("ground")])

    assert isinstance(selected, GatewayV2NoOpAction)


@pytest.mark.parametrize(
    ("allowed_actions", "expected_type"),
    [
        (["wait", "stop_hosting"], GatewayV2WaitAction),
        (["no_op", "stop_hosting"], GatewayV2NoOpAction),
        (["stop_hosting"], GatewayV2StopHostingAction),
    ],
)
def test_fallback_action_never_exceeds_lease_scope(
    allowed_actions: list[str],
    expected_type: type,
) -> None:
    context = build_gateway_v2_agent_context(
        _event(lease=_lease(allowed_actions=allowed_actions, available_skills=[], hints=[]))
    )

    selected = select_gateway_v2_action(context, [])

    assert isinstance(selected, expected_type)


def test_selector_raises_when_lease_has_no_permitted_fallback() -> None:
    context = build_gateway_v2_agent_context(
        _event(lease=_lease(allowed_actions=["call_skill"], available_skills=[], hints=[]))
    )

    with pytest.raises(GatewayV2DecisionSelectionError):
        select_gateway_v2_action(context, [])


def test_play_action_accepts_and_serializes_action_id() -> None:
    action = _skill("play_action", arguments={"actionId": "wave"})

    assert action.model_dump(by_alias=True)["arguments"] == {"actionId": "wave"}


def test_play_action_rejects_legacy_action_argument() -> None:
    with pytest.raises(ValidationError, match="actionId"):
        _skill("play_action", arguments={"action": "wave"})


def test_v1_play_action_still_accepts_legacy_action_argument() -> None:
    action = RecommendedAction.model_validate(
        {
            "skillName": "play_action",
            "schemaVersion": "v1",
            "arguments": {"action": "wave"},
            "reason": "legacy contract",
            "priority": "medium",
        }
    )

    assert action.arguments == {"action": "wave"}


def test_v2_prompt_explicitly_requires_action_id_and_dynamic_gateway_scope() -> None:
    assert "play_action.arguments.actionId" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "play_action.arguments.action " in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "availableSkills" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "skillArgumentHints" in GATEWAY_V2_ACTION_REASONING_SYSTEM


def test_v2_graph_excludes_tracking_and_memory_side_effect_nodes() -> None:
    graph = build_gateway_v2_decision_graph()

    assert {
        "fetch_snapshot",
        "retrieve_rag_context",
        "intent_inference",
        "goal_evaluation",
        "gather_context",
        "behavior_analysis",
        "gateway_v2_action_reasoning",
        "gateway_v2_select_action",
    } <= set(graph.nodes)
    assert "tracking_update" not in graph.nodes
    assert "memory_update" not in graph.nodes


@dataclass
class _Runner:
    result: dict[str, Any] | None = None
    failure: Exception | None = None
    calls: int = 0

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        del state
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result or {}


@dataclass
class _BlockingRunner:
    cancelled: bool = False

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        del state
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True
        return {}


async def test_agent_error_becomes_retryable_failure() -> None:
    wait = GatewayV2WaitAction(reason="fallback", waitMs=1_000)
    service = GatewayV2DecisionService(
        runner=_Runner(
            result={
                "errors": ["RAG unavailable"],
                "selected_action": wait.model_dump(mode="json", by_alias=True),
            }
        )
    )

    with pytest.raises(GatewayV2AgentExecutionError) as raised:
        await service.decide(
            build_gateway_v2_agent_context(_event()),
            user_id="user-1",
            tenant_id="tenant-1",
        )

    assert raised.value.retryable is True
    assert raised.value.stage == "agent"
    assert raised.value.category == "node_error"


async def test_agent_exception_becomes_structured_retryable_failure() -> None:
    service = GatewayV2DecisionService(runner=_Runner(failure=RuntimeError("provider secret detail")))

    with pytest.raises(GatewayV2AgentExecutionError) as raised:
        await service.decide(
            build_gateway_v2_agent_context(_event()),
            user_id="user-1",
            tenant_id="tenant-1",
        )

    assert str(raised.value) == "gateway v2 agent execution failed"
    assert raised.value.retryable is True
    assert raised.value.category == "execution_failed"


async def test_empty_agent_output_is_not_converted_to_successful_wait() -> None:
    service = GatewayV2DecisionService(runner=_Runner(result={"errors": [], "reasoned_actions": []}))

    with pytest.raises(GatewayV2AgentExecutionError) as raised:
        await service.decide(
            build_gateway_v2_agent_context(_event()),
            user_id="user-1",
            tenant_id="tenant-1",
        )

    assert raised.value.category == "empty_output"


async def test_hung_agent_is_cancelled_at_the_configured_timeout() -> None:
    runner = _BlockingRunner()
    service = GatewayV2DecisionService(runner=runner, timeout_seconds=0.01)

    with pytest.raises(GatewayV2AgentExecutionError) as raised:
        await asyncio.wait_for(
            service.decide(
                build_gateway_v2_agent_context(_event()),
                user_id="user-1",
                tenant_id="tenant-1",
            ),
            timeout=1,
        )

    assert raised.value.category == "timeout"
    assert runner.cancelled is True


@pytest.mark.parametrize(
    ("action", "expected_fields"),
    [
        (
            GatewayV2CallSkillAction.model_validate(
                {
                    "action": "call_skill",
                    "skillName": "play_action",
                    "schemaVersion": "v2",
                    "arguments": {"actionId": "wave"},
                    "reason": "wave now",
                    "ttlMs": 12_000,
                }
            ),
            {
                "action": "call_skill",
                "skillName": "play_action",
                "schemaVersion": "v2",
                "arguments": {"actionId": "wave"},
                "ttlMs": 12_000,
            },
        ),
        (
            GatewayV2WaitAction(reason="wait", waitMs=2_500, ttlMs=9_000),
            {"action": "wait", "waitMs": 2_500, "ttlMs": 9_000},
        ),
        (
            GatewayV2NoOpAction(reason="nothing", ttlMs=8_000),
            {"action": "no_op", "ttlMs": 8_000},
        ),
        (
            GatewayV2StopHostingAction(reason="stop", ttlMs=7_000),
            {"action": "stop_hosting", "ttlMs": 7_000},
        ),
    ],
)
def test_stable_decision_body_for_all_actions(action: Any, expected_fields: dict[str, Any]) -> None:
    context = build_gateway_v2_agent_context(_event())

    frozen = freeze_gateway_v2_decision("decision-stable-1", context, action)

    assert frozen.body_json == {
        "contractVersion": "llm-gateway-http-v2",
        "decisionId": "decision-stable-1",
        "decisionLeaseId": "lease-1",
        "stateVersion": 7,
        "controlGeneration": 3,
        **expected_fields,
    }
    assert frozen.body_bytes.decode("utf-8") == frozen.canonical_text
    assert len(frozen.body_hash) == 64
    assert frozen.decision.model_dump(mode="json", by_alias=True) == frozen.body_json


def test_complete_goal_tracking_metadata_is_available_only_as_an_atomic_group() -> None:
    complete = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "jump",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "tracked jump",
            "userId": "account-1",
            "actionType": "jump",
            "goalMetric": "jump_count",
            "goalValue": 2,
            "baselineValue": 1,
            "expectedHours": 1,
        }
    )
    partial = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "jump",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "partial metadata",
            "goalMetric": "jump_count",
        }
    )

    metadata = complete.tracking_metadata()
    assert metadata is not None
    assert metadata.goal_metric == "jump_count"
    assert partial.tracking_metadata() is None


@dataclass
class _PlanningRepository:
    stored: PlannedDecision | None = None
    plans: int = 0

    async def find_by_source_event(self, event: ClaimedGatewayEvent) -> PlannedDecision | None:
        del event
        return self.stored

    async def plan_decision(self, event, context, action) -> PlannedDecision:
        self.plans += 1
        frozen = freeze_gateway_v2_decision("decision-stable-1", context, action)
        self.stored = PlannedDecision(
            row_id=event.row_id,
            decision_id="decision-stable-1",
            decision_lease_id=context.decision_lease_id,
            action=action.action,
            request_body_json=frozen.body_json,
            request_body_bytes=frozen.body_bytes,
            body_hash=frozen.body_hash,
            action_tracking_id=None,
            created=True,
        )
        return self.stored


async def test_stable_source_event_retry_reuses_persisted_decision_without_agent_rerun() -> None:
    runner = _Runner(
        result={
            "errors": [],
            "selected_action": GatewayV2WaitAction(reason="wait", waitMs=1_000).model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    )
    service = GatewayV2DecisionService(runner=runner)
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(decision_service=service, repository=repository)
    claimed = _claimed_for_planner()
    context = build_gateway_v2_agent_context(claimed.event)

    first = await planner(claimed, context)
    second = await planner(claimed, context)

    assert first == second == EventProcessResult("succeeded")
    assert runner.calls == 1
    assert repository.plans == 1
    assert repository.stored is not None
    first_bytes = repository.stored.request_body_bytes
    first_hash = repository.stored.body_hash
    assert repository.stored.request_body_bytes == first_bytes
    assert repository.stored.body_hash == first_hash


@pytest.mark.parametrize(
    ("runner", "timeout_seconds", "expected_category"),
    [
        (_Runner(failure=RuntimeError("provider unavailable")), 1.0, "execution_failed"),
        (_Runner(result={"errors": [], "reasoned_actions": []}), 1.0, "empty_output"),
        (_BlockingRunner(), 0.01, "timeout"),
    ],
    ids=["exception", "empty-output", "timeout"],
)
async def test_agent_failures_become_durable_worker_retries(
    runner: _Runner | _BlockingRunner,
    timeout_seconds: float,
    expected_category: str,
) -> None:
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(
            runner=runner,
            timeout_seconds=timeout_seconds,
        ),
        repository=_PlanningRepository(),
    )
    claimed = _claimed_for_planner()

    result = await asyncio.wait_for(
        planner(claimed, build_gateway_v2_agent_context(claimed.event)),
        timeout=1,
    )

    assert result == EventProcessResult(
        "retryable_failed",
        error_stage="agent",
        error_category=expected_category,
    )
    assert planner.repository.plans == 0
