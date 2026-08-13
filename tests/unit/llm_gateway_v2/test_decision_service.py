from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from src.core.agents import gateway_v2 as gateway_v2_module
from src.core.agents.gateway_v2 import (
    _prompt_size_fields,
    build_gateway_v2_decision_graph,
    gateway_v2_action_reasoning_node,
)
from src.core.agents.gateway_v2_models import (
    GatewayV2ActionList,
    GatewayV2CallSkillAction,
    GatewayV2NoOpAction,
    GatewayV2StopHostingAction,
    GatewayV2WaitAction,
)
from src.core.agents.gateway_v2_prompts import (
    GATEWAY_V2_ACTION_REASONING_SYSTEM,
    GATEWAY_V2_ACTION_REASONING_USER,
)
from src.core.agents.models import RecommendedAction
from src.core.integration.llm_gateway_v2 import decision_service as decision_service_module
from src.core.integration.llm_gateway_v2.activity_plan import (
    ActivityPlanProposal,
    create_plaza_social_plan,
    materialize_activity_plan,
    record_step_terminal,
)
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanBinding,
    ActivityPlanContext,
)
from src.core.integration.llm_gateway_v2.auto_chat import (
    AutoChatMessage,
    AutoChatPermanentError,
    AutoChatRetryableError,
)
from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2AgentExecutionError,
    GatewayV2DecisionPlanner,
    GatewayV2DecisionSelectionError,
    GatewayV2DecisionService,
    _gateway_v2_activity_skill_action,
    build_gateway_v2_agent_context,
    freeze_gateway_v2_decision,
    select_gateway_v2_action,
)
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent, EventProcessResult
from src.core.integration.llm_gateway_v2.outbox_repository import PlannedDecision
from src.core.integration.llm_gateway_v2.scene_catalog import (
    SceneCatalog,
    SceneCoordinates,
    SceneTarget,
    load_default_scene_catalog,
)


def _lease(
    *,
    lease_kind: str = "observation",
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
                "AccountId": "account-1",
                "SessionId": "session-1",
                "SceneId": "scene-1",
                "State": "active",
                "SeatState": "standing",
                "Position": {"x": 1, "y": 2, "z": 3},
            },
            "availableSkills": descriptors,
            "skillArgumentHints": argument_hints,
            "lastSkillResult": None,
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
                "skillName": terminal.get("skillName", "jump"),
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


def _claimed_for_planner(event=None) -> ClaimedGatewayEvent:
    event = event or _event()
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
    assert context.lease_kind == "observation"
    assert context.allowed_decision_actions == ("call_skill", "wait", "no_op")
    assert context.allowed_skill_name == "jump"
    assert context.allowed_skill_names == ("jump",)
    assert context.parent_skill_name is None
    assert context.session_snapshot == {
        "AccountId": "account-1",
        "SessionId": "session-1",
        "SceneId": "scene-1",
        "State": "active",
        "SeatState": "standing",
        "Position": {"x": 1, "y": 2, "z": 3},
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


def test_observation_uses_available_skills_when_allowed_skill_names_is_empty() -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind="observation",
                allowed_skill_name=None,
                allowed_skill_names=[],
                available_skills=[{"skillName": "jump", "schemaVersion": "v1"}],
            )
        )
    )

    selected = select_gateway_v2_action(context, [_skill("jump")])

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == "jump"


@pytest.mark.parametrize("exit_skill", ["hot_air_balloon_exit", "helicopter_exit"])
def test_observation_never_initiates_vehicle_exit(exit_skill: str) -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind="observation",
                allowed_skill_name=None,
                allowed_skill_names=[],
                available_skills=[{"skillName": exit_skill, "schemaVersion": "v1"}],
                hints=[
                    {
                        "skillName": exit_skill,
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    }
                ],
            )
        )
    )

    selected = select_gateway_v2_action(context, [_skill(exit_skill)])

    assert isinstance(selected, GatewayV2WaitAction)


def test_vehicle_recovery_allows_observe_state_without_vehicle_pairing() -> None:
    context = build_gateway_v2_agent_context(
        _event(
            lease=_lease(
                lease_kind="vehicle_recovery",
                allowed_skill_name=None,
                allowed_skill_names=["observe_state", "hot_air_balloon_exit"],
                parent_skill_name="hot_air_balloon_auto_schedule",
                available_skills=[
                    {"skillName": "observe_state", "schemaVersion": "v1"},
                    {"skillName": "hot_air_balloon_exit", "schemaVersion": "v1"},
                ],
                hints=[
                    {
                        "skillName": name,
                        "schemaVersion": "v1",
                        "allowedArgs": [],
                        "missingArgs": [],
                    }
                    for name in ("observe_state", "hot_air_balloon_exit")
                ],
            )
        )
    )

    selected = select_gateway_v2_action(context, [_skill("observe_state")])

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == "observe_state"


def test_unknown_lease_kind_never_allows_call_skill() -> None:
    context = build_gateway_v2_agent_context(
        _event(lease=_lease(lease_kind="future_unknown_lease"))
    )

    selected = select_gateway_v2_action(context, [_skill("jump")])

    assert isinstance(selected, GatewayV2WaitAction)


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


def test_paper_plane_activity_action_replaces_gateway_suggested_arguments() -> None:
    payload = _lease(
        available_skills=[
            {"skillName": "paper_plane_auto_schedule", "schemaVersion": "v1"}
        ],
        hints=[
            {
                "skillName": "paper_plane_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["planeName", "useTimeMs", "isComplete"],
                "missingArgs": ["planeName", "useTimeMs", "isComplete"],
            }
        ],
    )
    payload["decisionContext"]["skillArgumentHints"][0]["suggestedArgs"] = {
        "planeName": "纸飞机A",
        "useTimeMs": 12,
        "isComplete": False,
    }
    context = build_gateway_v2_agent_context(_event(lease=payload))

    action = _gateway_v2_activity_skill_action(
        context,
        "paper_plane_auto_schedule",
        "v1",
        reason="paper plane",
    )

    assert action is not None
    assert action.arguments != {"planeName": "纸飞机A", "useTimeMs": 12, "isComplete": False}
    assert action.arguments["planeName"] in {"初级", "中级", "高级"}
    minimum, maximum = {
        "初级": (100_000, 200_000),
        "中级": (90_000, 180_000),
        "高级": (70_000, 130_000),
    }[action.arguments["planeName"]]
    assert minimum <= action.arguments["useTimeMs"] <= maximum
    assert action.arguments["isComplete"] is True


@pytest.mark.parametrize(
    ("skill_name", "allowed_args", "bad_suggested"),
    [
        (
            "darts_auto_schedule",
            ["score", "darts", "allowPurchaseWhenInsufficient"],
            {"score": 120, "darts": [], "allowPurchaseWhenInsufficient": True},
        ),
        (
            "shooting_auto_schedule",
            ["distance", "weapon", "posture", "score"],
            {"distance": "invalid", "weapon": "pistol", "posture": "prone", "score": 86},
        ),
    ],
)
def test_competitive_activity_replaces_invalid_gateway_suggestions(
    skill_name: str,
    allowed_args: list[str],
    bad_suggested: dict[str, Any],
) -> None:
    payload = _lease(
        available_skills=[{"skillName": skill_name, "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": skill_name,
                "schemaVersion": "v1",
                "allowedArgs": allowed_args,
                "missingArgs": allowed_args,
            }
        ],
    )
    payload["decisionContext"]["skillArgumentHints"][0]["suggestedArgs"] = bad_suggested
    context = build_gateway_v2_agent_context(_event(lease=payload))

    action = _gateway_v2_activity_skill_action(context, skill_name, "v1", reason="activity")

    assert action is not None
    assert action.arguments != bad_suggested
    if skill_name == "darts_auto_schedule":
        assert 1 <= action.arguments["score"] <= 50
        assert sum(item["count"] for item in action.arguments["darts"]) == 9
        assert action.arguments["allowPurchaseWhenInsufficient"] is False
    else:
        assert (
            action.arguments["distance"],
            action.arguments["weapon"],
            action.arguments["posture"],
        ) in {
            ("10m", "pistol", "standing"),
            ("10m", "rifle", "standing"),
            ("25m", "pistol", "standing"),
            ("50m", "rifle", "standing"),
            ("50m", "rifle", "crouching"),
            ("50m", "rifle", "prone"),
        }
        assert 30 <= action.arguments["score"] <= 80


def test_dance_activity_uses_fixed_product_score_range_without_gateway_score_range() -> None:
    payload = _lease(
        available_skills=[{"skillName": "dance_auto_schedule", "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": "dance_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["score"],
                "missingArgs": ["score"],
            }
        ],
    )
    context_without_range = build_gateway_v2_agent_context(_event(lease=payload))

    action = _gateway_v2_activity_skill_action(
        context_without_range,
        "dance_auto_schedule",
        "v1",
        reason="dance",
    )

    assert action is not None
    assert set(action.arguments) == {"score"}
    assert 70 <= action.arguments["score"] <= 120


def test_selector_accepts_dance_without_external_score_range() -> None:
    payload = _lease(
        available_skills=[
            {"skillName": "dance_auto_schedule", "schemaVersion": "v1"},
            {"skillName": "jump", "schemaVersion": "v1"},
        ],
        hints=[
            {
                "skillName": "dance_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["score"],
                "missingArgs": ["score"],
            },
            {
                "skillName": "jump",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            },
        ],
    )
    context = build_gateway_v2_agent_context(_event(lease=payload))

    selected = select_gateway_v2_action(
        context,
        [
            GatewayV2CallSkillAction(
                action="call_skill",
                skillName="dance_auto_schedule",
                schemaVersion="v1",
                arguments={},
            ),
        ],
    )

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == "dance_auto_schedule"
    assert 70 <= selected.arguments["score"] <= 120


@pytest.mark.parametrize(
    ("skill_name", "allowed_args", "bad_arguments"),
    [
        (
            "darts_auto_schedule",
            ["score", "darts", "allowPurchaseWhenInsufficient"],
            {"score": 120},
        ),
        (
            "shooting_auto_schedule",
            ["distance", "weapon", "posture", "score"],
            {"distance": "10m", "weapon": "pistol", "posture": "standing", "score": 86},
        ),
    ],
)
def test_freeze_applies_final_competitive_activity_outbound_fallback(
    skill_name: str,
    allowed_args: list[str],
    bad_arguments: dict[str, Any],
) -> None:
    payload = _lease(
        available_skills=[{"skillName": skill_name, "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": skill_name,
                "schemaVersion": "v1",
                "allowedArgs": allowed_args,
                "missingArgs": allowed_args,
            }
        ],
    )
    context = build_gateway_v2_agent_context(_event(lease=payload))
    action = GatewayV2CallSkillAction(
        action="call_skill",
        skillName=skill_name,
        schemaVersion="v1",
        arguments=bad_arguments,
    )

    frozen = freeze_gateway_v2_decision("decision-fallback", "trace-fallback", context, action)

    assert frozen.body_json["action"] == "call_skill"
    assert frozen.body_json["arguments"] != bad_arguments


def test_freeze_keeps_dance_with_fixed_score_without_gateway_range() -> None:
    payload = _lease(
        available_skills=[{"skillName": "dance_auto_schedule", "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": "dance_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["score"],
                "missingArgs": ["score"],
            }
        ],
    )
    context = build_gateway_v2_agent_context(_event(lease=payload))
    action = GatewayV2CallSkillAction(
        action="call_skill",
        skillName="dance_auto_schedule",
        schemaVersion="v1",
        arguments={},
    )

    frozen = freeze_gateway_v2_decision("decision-dance", "trace-dance", context, action)

    assert frozen.body_json["action"] == "call_skill"
    assert frozen.body_json["skillName"] == "dance_auto_schedule"
    assert 70 <= frozen.body_json["arguments"]["score"] <= 120


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
    assert "json" in GATEWAY_V2_ACTION_REASONING_SYSTEM.lower()
    assert '"reason"' in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert '"waitMs"' in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert '"ttlMs"' in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "autonomously hosted" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "Do not wait for a user request" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "scene_tornado" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "observe_state" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "current activity plan step" in GATEWAY_V2_ACTION_REASONING_SYSTEM
    assert "Do not immediately repeat" in GATEWAY_V2_ACTION_REASONING_SYSTEM


def test_v2_prompt_json_examples_do_not_become_template_variables() -> None:
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", GATEWAY_V2_ACTION_REASONING_SYSTEM),
            ("human", GATEWAY_V2_ACTION_REASONING_USER),
        ]
    )

    assert set(prompt.input_variables) == {
        "behavior_report",
        "activity_plan",
        "current_phase",
        "current_step",
        "enriched_context",
        "gateway_context",
        "goal_evaluation_result",
        "intent_result",
        "rag_context",
        "recent_action_history",
        "recent_failure_history",
        "snapshot_text",
    }


def test_action_list_defaults_model_omitted_non_wire_metadata() -> None:
    action_list = GatewayV2ActionList.model_validate(
        {
            "actions": [
                {
                    "action": "call_skill",
                    "skillName": "observe_state",
                    "schemaVersion": "v1",
                    "arguments": {},
                    "ttlMs": 30_000,
                },
                {
                    "action": "wait",
                    "ttlMs": 30_000,
                },
                {
                    "action": "no_op",
                    "ttlMs": 30_000,
                },
            ]
        }
    )

    assert [action.reason for action in action_list.actions] == [
        "Model selected this action",
        "Model selected this action",
        "Model selected this action",
    ]
    wait = action_list.actions[1]
    assert isinstance(wait, GatewayV2WaitAction)
    assert wait.wait_ms == 1_000


def test_prompt_size_fields_log_lengths_without_prompt_content() -> None:
    fields = _prompt_size_fields("rag_context", "private prompt content")

    assert fields["rag_context_chars"] == len("private prompt content")
    assert fields["rag_context_estimated_tokens"] > 0
    assert "private prompt content" not in str(fields)


async def test_action_reasoning_logs_prompt_size_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _event()
    context = build_gateway_v2_agent_context(event)

    class _StructuredOutputStub:
        def with_structured_output(self, schema: Any, *, method: str) -> RunnableLambda:
            assert schema is GatewayV2ActionList
            assert method == "json_mode"
            return RunnableLambda(lambda _: GatewayV2ActionList(actions=(_skill("jump"),)))

    async def fake_get_llm(*, model_type: str) -> _StructuredOutputStub:
        assert model_type == "default"
        return _StructuredOutputStub()

    monkeypatch.setattr(gateway_v2_module, "get_llm", fake_get_llm)
    caplog.set_level(logging.INFO, logger="src.core.agents.gateway_v2")

    result = await gateway_v2_action_reasoning_node(
        {
            "gateway_context": context.model_dump(mode="json"),
            "snapshot": context.prompt_payload()["session"],
            "rag_context": "private rag content",
            "behavior_report": "",
            "enriched_context": "",
            "intent_result": {},
            "goal_evaluation_result": {},
        }
    )

    assert result.get("errors", []) == []
    record = next(
        item
        for item in caplog.records
        if item.getMessage().startswith("[gateway_v2] prompt size breakdown:")
    )
    assert record.gateway_context_estimated_tokens > 0
    assert record.skill_catalog_estimated_tokens > 0
    assert record.rag_context_chars == len("private rag content")
    assert record.final_prompt_estimated_tokens > 0
    assert "gateway_context_estimated_tokens=" in caplog.text
    assert "skill_catalog_estimated_tokens=" in caplog.text
    assert "rag_context_estimated_tokens=" in caplog.text
    assert "final_prompt_estimated_tokens=" in caplog.text
    assert "private rag content" not in caplog.text


async def test_action_reasoning_logs_safe_failure_metadata(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _event()
    context = build_gateway_v2_agent_context(event)

    class _FailingStructuredOutputStub:
        def with_structured_output(self, schema: Any, *, method: str) -> RunnableLambda:
            assert schema is GatewayV2ActionList
            assert method == "json_mode"

            async def fail(_: Any) -> Any:
                raise RuntimeError("private model response")

            return RunnableLambda(fail)

    async def fake_get_llm(*, model_type: str) -> _FailingStructuredOutputStub:
        assert model_type == "default"
        return _FailingStructuredOutputStub()

    monkeypatch.setattr(gateway_v2_module, "get_llm", fake_get_llm)
    caplog.set_level(logging.ERROR, logger="src.core.agents.gateway_v2")

    result = await gateway_v2_action_reasoning_node(
        {
            "gateway_context": context.model_dump(mode="json"),
            "snapshot": context.prompt_payload()["session"],
            "rag_context": "private rag content",
        }
    )

    assert result["errors"] == ["gateway v2 action reasoning failed"]
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "[gateway_v2] action reasoning failed"
    )
    assert record.exception_type == "RuntimeError"
    assert record.event_id == context.event_id
    assert "private model response" not in caplog.text
    assert "private rag content" not in caplog.text


async def test_action_reasoning_retries_one_invalid_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_gateway_v2_agent_context(_event())

    class _FlakyStructuredOutputStub:
        calls = 0

        def with_structured_output(self, schema: Any, *, method: str) -> RunnableLambda:
            assert schema is GatewayV2ActionList
            assert method == "json_mode"

            async def invoke(_: Any) -> GatewayV2ActionList:
                self.calls += 1
                if self.calls == 1:
                    raise ValidationError.from_exception_data("GatewayV2ActionList", [])
                return GatewayV2ActionList(actions=(_skill("jump"),))

            return RunnableLambda(invoke)

    model = _FlakyStructuredOutputStub()

    async def fake_get_llm(*, model_type: str) -> _FlakyStructuredOutputStub:
        assert model_type == "default"
        return model

    monkeypatch.setattr(gateway_v2_module, "get_llm", fake_get_llm)

    result = await gateway_v2_action_reasoning_node(
        {
            "gateway_context": context.model_dump(mode="json"),
            "snapshot": context.prompt_payload()["session"],
            "rag_context": "No RAG context",
        }
    )

    assert model.calls == 2
    assert result.get("errors", []) == []
    assert result["reasoned_actions"][0]["skillName"] == "jump"


def test_v2_graph_uses_only_realtime_decision_nodes() -> None:
    graph = build_gateway_v2_decision_graph()

    assert set(graph.nodes) == {
        "fetch_snapshot",
        "retrieve_rag_context",
        "gateway_v2_action_reasoning",
        "gateway_v2_select_action",
    }


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


async def test_decision_service_injects_activity_plan_context_into_graph_state() -> None:
    captured: dict[str, Any] = {}

    class _CapturingRunner:
        async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
            captured.update(state)
            return {
                "errors": [],
                "selected_action": GatewayV2WaitAction(
                    reason="wait", waitMs=1_000
                ).model_dump(mode="json", by_alias=True),
            }

    plan = create_plaza_social_plan("plan-state")
    activity_context = ActivityPlanContext(
        plan=plan,
        binding=ActivityPlanBinding("plan-state", 1, "arrival", "arrival"),
        recent_actions=({"decision_id": "decision-1"},),
        recent_failures=({"decision_id": "decision-failed"},),
    )

    result = await GatewayV2DecisionService(runner=_CapturingRunner()).decide(
        build_gateway_v2_agent_context(_event()),
        user_id="account-1",
        tenant_id="tenant-1",
        activity_context=activity_context,
    )

    assert result.action == "wait"
    assert captured["activity_plan"] == plan.model_dump(mode="json", by_alias=True)
    assert captured["current_phase"] == "arrival"
    assert captured["current_step"] == plan.current_step().model_dump(mode="json", by_alias=True)
    assert captured["recent_action_history"] == [{"decision_id": "decision-1"}]
    assert captured["recent_failure_history"] == [{"decision_id": "decision-failed"}]


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

    frozen = freeze_gateway_v2_decision("decision-stable-1", "trace-source-1", context, action)

    assert frozen.body_json == {
        "traceId": "trace-source-1",
        "contractVersion": "llm-gateway-http-v2",
        "sessionId": "session-1",
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
    activity_bindings: list[ActivityPlanBinding | None] = None

    def __post_init__(self) -> None:
        if self.activity_bindings is None:
            self.activity_bindings = []

    async def find_by_source_event(self, event: ClaimedGatewayEvent) -> PlannedDecision | None:
        del event
        return self.stored

    async def plan_decision(self, event, context, action, activity_binding=None) -> PlannedDecision:
        self.plans += 1
        assert self.activity_bindings is not None
        self.activity_bindings.append(activity_binding)
        frozen = freeze_gateway_v2_decision("decision-stable-1", event.trace_id, context, action)
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


@dataclass
class _ActivityCoordinator:
    context: ActivityPlanContext
    calls: int = 0

    async def prepare(self, event, context) -> ActivityPlanContext:
        del event, context
        self.calls += 1
        return self.context


def _move_activity_context(target_id: str) -> ActivityPlanContext:
    plan = materialize_activity_plan(
        ActivityPlanProposal.model_validate(
            {
                "goalId": "plaza_wander",
                "goalSummary": "Walk around the plaza before continuing activities",
                "steps": [
                    {
                        "stepId": f"wander-{index}",
                        "phase": "movement",
                        "skillName": "move_to",
                        "schemaVersion": "v1",
                        "sceneTargetId": target_id,
                        "intent": "Walk to an indexed scene point",
                    }
                    for index in range(1, 4)
                ],
            }
        ),
        plan_id="plan-wander",
        version=1,
        available_skills={("move_to", "v1")},
        lobby=False,
    )
    return ActivityPlanContext(
        plan=plan,
        binding=ActivityPlanBinding("plan-wander", 1, "wander-1", "movement"),
        recent_actions=(),
        recent_failures=(),
    )


@dataclass
class _ConversationService:
    calls: int = 0

    async def decide(self, context) -> GatewayV2CallSkillAction:
        self.calls += 1
        return GatewayV2CallSkillAction(
            action="call_skill",
            skillName="nearby_chat_send",
            schemaVersion="v1",
            arguments={
                "conversationId": "conv-10001-10002-1",
                "targetRoleId": 10002,
                "content": "我们去前面看看。",
            },
            reason="Auto Chat generated a nearby conversation message",
            ttlMs=10_000,
        )


def _conversation_event():
    payload = _lease(
        lease_kind="conversation",
        allowed_actions=["call_skill"],
        available_skills=[
            {"skillName": "nearby_chat_send", "schemaVersion": "v1"}
        ],
        hints=[
            {
                "skillName": "nearby_chat_send",
                "schemaVersion": "v1",
                "allowedArgs": ["conversationId", "targetRoleId", "content"],
                "missingArgs": ["conversationId", "targetRoleId", "content"],
            }
        ],
        allowed_skill_name="nearby_chat_send",
        allowed_skill_names=["nearby_chat_send"],
    )
    payload["decisionContext"]["session"]["conversation"] = {
        "conversationId": "conv-10001-10002-1",
        "pairKey": "10001:10002",
        "speakerRoleId": 10001,
        "targetRoleId": 10002,
        "brainUsername": "conv-10001",
        "historyRounds": [],
        "completedRounds": 0,
        "maxRounds": 6,
        "expiresAtMs": 1_800_000_000_000,
    }
    return _event(lease=payload)


async def test_conversation_lease_is_not_a_decision_skill() -> None:
    runner = _Runner(result={"errors": ["must not run"]})
    conversation_service = _ConversationService()
    repository = _PlanningRepository()
    event = _conversation_event()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        conversation_service=conversation_service,
        repository=repository,
    )

    result = await planner(
        _claimed_for_planner(event),
        build_gateway_v2_agent_context(event),
    )

    assert result == EventProcessResult(
        "manual",
        error_stage="chat",
        error_category="conversation_lease_not_supported",
    )
    assert runner.calls == 0
    assert conversation_service.calls == 0
    assert repository.stored is None


@dataclass
class _AutoChatStub:
    result: AutoChatMessage | None = None
    failure: Exception | None = None
    calls: int = 0

    async def generate(self, conversation) -> AutoChatMessage:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert self.result is not None
        return self.result


def _auto_chat_message() -> AutoChatMessage:
    return AutoChatMessage.model_validate(
        {
            "speakerRoleId": 10001,
            "targetRoleId": 10002,
            "pairKey": "10001:10002",
            "content": "我们去前面看看。",
            "summaryVersion": 1,
            "summaryUpdatedAt": None,
        }
    )


async def test_auto_chat_decision_service_builds_only_authorized_chat_action() -> None:
    client = _AutoChatStub(result=_auto_chat_message())
    service_type = decision_service_module.GatewayV2AutoChatDecisionService
    service = service_type(
        client=client,
        decision_ttl_ms=10_000,
        now_ms=lambda: 1_799_999_950_000,
    )
    context = build_gateway_v2_agent_context(_conversation_event())

    action = await service.decide(context)

    assert client.calls == 1
    assert action.model_dump(mode="json", by_alias=True) == {
        "reason": "Auto Chat generated a nearby conversation message",
        "ttlMs": 10_000,
        "action": "call_skill",
        "skillName": "nearby_chat_send",
        "schemaVersion": "v1",
        "arguments": {
            "conversationId": "conv-10001-10002-1",
            "targetRoleId": 10002,
            "content": "我们去前面看看。",
        },
        "userId": None,
        "actionType": None,
        "goalMetric": None,
        "goalValue": None,
        "baselineValue": None,
        "expectedHours": None,
    }


async def test_auto_chat_decision_service_logs_metadata_without_message_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _AutoChatStub(result=_auto_chat_message())
    service = decision_service_module.GatewayV2AutoChatDecisionService(
        client=client,
        now_ms=lambda: 1_799_999_950_000,
    )
    caplog.set_level(
        logging.INFO,
        logger="src.core.integration.llm_gateway_v2.decision_service",
    )

    await service.decide(build_gateway_v2_agent_context(_conversation_event()))

    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "Auto Chat conversation message generated"
    )
    assert record.conversation_id == "conv-10001-10002-1"
    assert record.speaker_role_id == 10001
    assert record.target_role_id == 10002
    assert record.elapsed_ms >= 0
    assert "我们去前面看看。" not in caplog.text


@pytest.mark.parametrize(
    ("failure", "expected_outcome"),
    [
        (
            AutoChatRetryableError("timeout"),
            EventProcessResult("manual", error_stage="chat", error_category="conversation_lease_not_supported"),
        ),
        (
            AutoChatPermanentError("response_identity_mismatch"),
            EventProcessResult("manual", error_stage="chat", error_category="conversation_lease_not_supported"),
        ),
    ],
)
async def test_planner_preserves_auto_chat_error_retryability(
    failure: Exception,
    expected_outcome: EventProcessResult,
) -> None:
    client = _AutoChatStub(failure=failure)
    service = decision_service_module.GatewayV2AutoChatDecisionService(
        client=client,
        now_ms=lambda: 1_799_999_950_000,
    )
    event = _conversation_event()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=_Runner()),
        conversation_service=service,
        repository=_PlanningRepository(),
    )

    result = await planner(
        _claimed_for_planner(event),
        build_gateway_v2_agent_context(event),
    )

    assert result == expected_outcome


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


async def test_planner_prepares_activity_context_and_binds_decision() -> None:
    event = _event()
    plan = create_plaza_social_plan("plan-1")
    activity_context = ActivityPlanContext(
        plan=plan,
        binding=ActivityPlanBinding("plan-1", 1, "arrival", "arrival"),
        recent_actions=(),
        recent_failures=(),
    )
    coordinator = _ActivityCoordinator(activity_context)
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(
            runner=_Runner(
                result={
                    "errors": [],
                    "selected_action": GatewayV2WaitAction(
                        reason="wait", waitMs=1_000
                    ).model_dump(mode="json", by_alias=True),
                }
            )
        ),
        repository=repository,
        activity_coordinator=coordinator,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert coordinator.calls == 1
    assert repository.activity_bindings == [activity_context.binding]


async def test_planner_executes_current_activity_step_without_second_model_call() -> None:
    payload = _lease(
        available_skills=[{"skillName": "dance_auto_schedule", "schemaVersion": "v1"}],
        hints=[
                {
                    "skillName": "dance_auto_schedule",
                    "schemaVersion": "v1",
                    "allowedArgs": ["score"],
                    "missingArgs": ["score"],
                }
            ],
        )
    event = _event(lease=payload)
    plan = record_step_terminal(
        create_plaza_social_plan("plan-dance"),
        "arrival",
        succeeded=True,
    )
    activity_context = ActivityPlanContext(
        plan=plan,
        binding=ActivityPlanBinding("plan-dance", 1, "dance", "activity"),
        recent_actions=(),
        recent_failures=(),
    )
    coordinator = _ActivityCoordinator(activity_context)
    repository = _PlanningRepository()
    runner = _Runner(result={"errors": ["must not run"]})
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        activity_coordinator=coordinator,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json["skillName"] == "dance_auto_schedule"
    assert repository.activity_bindings == [activity_context.binding]


def _forced_activity_lease() -> dict[str, Any]:
    return _lease(
        available_skills=[
            {"skillName": "paper_plane_auto_schedule", "schemaVersion": "v1"},
            {"skillName": "darts_auto_schedule", "schemaVersion": "v1"},
            {"skillName": "dance_auto_schedule", "schemaVersion": "v1"},
        ],
        hints=[
            {
                "skillName": "paper_plane_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["planeName", "useTimeMs", "isComplete"],
                "missingArgs": ["planeName", "useTimeMs", "isComplete"],
            },
            {
                "skillName": "darts_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["score", "darts", "allowPurchaseWhenInsufficient"],
                "missingArgs": ["score", "darts", "allowPurchaseWhenInsufficient"],
            },
            {
                "skillName": "dance_auto_schedule",
                "schemaVersion": "v1",
                "allowedArgs": ["score"],
                "missingArgs": ["score"],
            },
        ],
    )


async def test_force_skills_bypasses_activity_plan_and_agent_with_contract_arguments() -> None:
    event = _event(lease=_forced_activity_lease())
    plan = record_step_terminal(create_plaza_social_plan("plan-force"), "arrival", succeeded=True)
    coordinator = _ActivityCoordinator(
        ActivityPlanContext(
            plan=plan,
            binding=ActivityPlanBinding("plan-force", 1, "dance", "activity"),
            recent_actions=(),
            recent_failures=(),
        )
    )
    runner = _Runner(result={"errors": ["must not run"]})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        activity_coordinator=coordinator,
        force_skills=("paper_plane_auto_schedule", "darts_auto_schedule"),
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert coordinator.calls == 0
    assert runner.calls == 0
    assert repository.stored is not None
    body = repository.stored.request_body_json
    assert body["skillName"] == "paper_plane_auto_schedule"
    assert body["arguments"]["planeName"] in {"初级", "中级", "高级"}
    assert body["arguments"]["isComplete"] is True


async def test_force_skills_rotates_after_successful_skill_finished() -> None:
    event = _event(
        lease=_forced_activity_lease(),
        terminal={"status": "success", "skillName": "paper_plane_auto_schedule"},
    )
    runner = _Runner(result={"errors": ["must not run"]})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        force_skills=("paper_plane_auto_schedule", "darts_auto_schedule"),
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    body = repository.stored.request_body_json
    assert body["skillName"] == "darts_auto_schedule"
    assert 1 <= body["arguments"]["score"] <= 50
    assert sum(item["count"] for item in body["arguments"]["darts"]) == 9
    assert body["arguments"]["allowPurchaseWhenInsufficient"] is False


async def test_force_skills_waits_when_gateway_lease_publishes_none_of_them() -> None:
    event = _event()
    runner = _Runner(result={"errors": ["must not run"]})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        force_skills=("paper_plane_auto_schedule", "darts_auto_schedule"),
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json["action"] == "wait"
    assert repository.stored.request_body_json["waitMs"] == 1_000


async def test_planner_resolves_scene_target_to_trusted_move_coordinates() -> None:
    payload = _lease(
        available_skills=[{"skillName": "move_to", "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": "move_to",
                "schemaVersion": "v1",
                "allowedArgs": ["target.x", "target.y", "target.z"],
                "missingArgs": ["target.x", "target.y", "target.z"],
            }
        ],
    )
    payload["decisionContext"]["session"].update(
        {"SceneId": 7, "SceneName": "CJ_guangchang"}
    )
    event = _event(lease=payload)
    activity_context = _move_activity_context(
        "scene:7:activity:wish_board:458"
    )
    scene_catalog = SceneCatalog(
        [
            SceneTarget(
                target_id="scene:7:activity:wish_board:458",
                scene_id=7,
                scene_name="CJ_guangchang",
                kind="activity",
                activity="wish_board",
                point_key="458",
                coordinates=SceneCoordinates(100.519966, 1.15435553, -25.9959488),
                source_path="wish-board-458",
            )
        ]
    )
    runner = _Runner(result={"errors": ["must not run"]})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        activity_coordinator=_ActivityCoordinator(activity_context),
        scene_catalog=scene_catalog,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json["skillName"] == "move_to"
    assert repository.stored.request_body_json["arguments"] == {
        "target": {"x": 100.519966, "y": 1.15435553, "z": -25.9959488}
    }
    assert "sceneTargetId" not in repository.stored.request_body_json


async def test_planner_rejects_non_walkable_shooting_table_as_move_target() -> None:
    payload = _lease(
        available_skills=[{"skillName": "move_to", "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": "move_to",
                "schemaVersion": "v1",
                "allowedArgs": ["target.x", "target.y", "target.z"],
                "missingArgs": ["target.x", "target.y", "target.z"],
            }
        ],
    )
    payload["decisionContext"]["session"].update(
        {"SceneId": 8, "SceneName": "CJ_JiuBa_Zhong_suo"}
    )
    event = _event(lease=payload)
    runner = _Runner(result={"errors": ["must not run"]})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        activity_coordinator=_ActivityCoordinator(
            _move_activity_context("scene:8:shooting:10")
        ),
        scene_catalog=SceneCatalog(
            [
                SceneTarget(
                    target_id="scene:8:shooting:10",
                    scene_id=8,
                    scene_name="CJ_JiuBa_Zhong_suo",
                    kind="shooting",
                    activity="shooting",
                    point_key="10",
                    coordinates=SceneCoordinates(-13.004, 0.011, 42.287),
                    source_path="shooting-table-10",
                )
            ]
        ),
    )

    result = await planner(
        _claimed_for_planner(event),
        build_gateway_v2_agent_context(event),
    )

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json["action"] == "wait"
    assert repository.stored.request_body_json["waitMs"] == 1_000


@pytest.mark.parametrize(
    ("scene_id", "scene_name", "expected_unique_targets"),
    [
        (7, "CJ_guangchang", 10),
        (8, "CJ_JiuBa_Zhong_suo", 10),
        (9, "CJ_JiuBa_Ce_suo", 10),
    ],
)
async def test_ten_roles_emit_diverse_trusted_move_coordinates_from_packaged_scenes(
    scene_id: int,
    scene_name: str,
    expected_unique_targets: int,
) -> None:
    scene_catalog = load_default_scene_catalog()
    wire_targets: list[tuple[float, float, float]] = []

    for index in range(10):
        role_id = f"role-{index}"
        target = scene_catalog.select_candidates(
            scene_id=scene_id,
            role_identity=role_id,
            plan_version=1,
            limit=1,
        )[0]
        payload = _lease(
            available_skills=[{"skillName": "move_to", "schemaVersion": "v1"}],
            hints=[
                {
                    "skillName": "move_to",
                    "schemaVersion": "v1",
                    "allowedArgs": ["target.x", "target.y", "target.z"],
                    "missingArgs": ["target.x", "target.y", "target.z"],
                }
            ],
        )
        payload["decisionContext"]["session"].update(
            {
                "AccountId": role_id,
                "RoleId": role_id,
                "SceneId": scene_id,
                "SceneName": scene_name,
            }
        )
        event = _event(lease=payload)
        repository = _PlanningRepository()
        runner = _Runner(result={"errors": ["must not run"]})
        planner = GatewayV2DecisionPlanner(
            decision_service=GatewayV2DecisionService(runner=runner),
            repository=repository,
            activity_coordinator=_ActivityCoordinator(
                _move_activity_context(target.target_id)
            ),
            scene_catalog=scene_catalog,
        )

        result = await planner(
            _claimed_for_planner(event),
            build_gateway_v2_agent_context(event),
        )

        assert result == EventProcessResult("succeeded")
        assert runner.calls == 0
        assert repository.stored is not None
        body = repository.stored.request_body_json
        assert body["action"] == "call_skill"
        assert body["skillName"] == "move_to"
        assert body["schemaVersion"] == "v1"
        assert body["arguments"] == {
            "target": target.coordinates.as_arguments()
        }
        assert "sceneTargetId" not in body
        coordinates = body["arguments"]["target"]
        wire_targets.append(
            (coordinates["x"], coordinates["y"], coordinates["z"])
        )

    assert len(set(wire_targets)) == expected_unique_targets


async def test_planner_never_asks_model_to_invent_missing_scene_coordinates() -> None:
    payload = _lease(
        available_skills=[{"skillName": "move_to", "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": "move_to",
                "schemaVersion": "v1",
                "allowedArgs": ["target.x", "target.y", "target.z"],
                "missingArgs": ["target.x", "target.y", "target.z"],
            }
        ],
    )
    payload["decisionContext"]["session"].update(
        {"SceneId": 7, "SceneName": "CJ_guangchang"}
    )
    event = _event(lease=payload)
    runner = _Runner(
        result={
            "errors": [],
            "selected_action": GatewayV2CallSkillAction(
                action="call_skill",
                skillName="move_to",
                schemaVersion="v1",
                arguments={"target": {"x": 999, "y": 999, "z": 999}},
                reason="invented coordinates",
            ).model_dump(mode="json", by_alias=True),
        }
    )
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
        activity_coordinator=_ActivityCoordinator(
            _move_activity_context("scene:7:activity:wish_board:missing")
        ),
        scene_catalog=SceneCatalog([]),
    )

    result = await planner(
        _claimed_for_planner(event),
        build_gateway_v2_agent_context(event),
    )

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json["action"] == "wait"
    assert "arguments" not in repository.stored.request_body_json


async def test_lobby_session_started_bootstraps_scene_tornado_without_agent_call() -> None:
    session_id = "3574531302836404224"
    decision_lease_id = "lease-3574531302836404224-1195"
    decision_payload = _lease(
        available_skills=[
            {"skillName": "observe_state", "schemaVersion": "v1"},
            {"skillName": "scene_tornado", "schemaVersion": "v1"},
        ],
        hints=[
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            },
            {
                "skillName": "scene_tornado",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            },
        ],
        allowed_skill_names=[],
    )
    decision_payload["lease"].update(
        {
            "sessionId": session_id,
            "controlGeneration": 822,
            "decisionLeaseId": decision_lease_id,
            "stateVersion": 822,
        }
    )
    decision_payload["decisionContext"]["session"].update(
        {
            "AccountId": "1270099452413083648",
            "SessionId": session_id,
            "State": 4,
            "SceneId": 1,
            "SceneName": "Lobby",
            "NavigationAvailable": False,
            "SkillExecuting": False,
            "LastSkillName": None,
        }
    )
    event = parse_gateway_v2_event(
        {
            "eventId": "llm-event-3574531302836404224-822-1",
            "eventType": "session_started",
            "sessionId": session_id,
            "controlGeneration": 822,
            "eventSequence": 1,
            "stateVersion": 822,
            "decisionLeaseId": decision_lease_id,
            "occurredAtMs": 1_785_914_230_816,
            "payload": decision_payload,
        }
    )
    runner = _Runner(result={})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json == {
        "traceId": "trace-1",
        "contractVersion": "llm-gateway-http-v2",
        "sessionId": session_id,
        "decisionId": "decision-stable-1",
        "decisionLeaseId": decision_lease_id,
        "stateVersion": 822,
        "controlGeneration": 822,
        "ttlMs": 30_000,
        "action": "call_skill",
        "skillName": "scene_tornado",
        "schemaVersion": "v1",
        "arguments": {},
    }


async def test_lobby_observation_bootstraps_scene_tornado_for_running_session() -> None:
    payload = _lease(
        available_skills=[
            {"skillName": "observe_state", "schemaVersion": "v1"},
            {"skillName": "scene_tornado", "schemaVersion": "v1"},
        ],
        hints=[
            {
                "skillName": name,
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            }
            for name in ("observe_state", "scene_tornado")
        ],
        allowed_skill_names=[],
    )
    payload["decisionContext"]["session"].update(
        {
            "SceneId": 1,
            "SceneName": "Lobby",
            "NavigationAvailable": False,
            "SkillExecuting": False,
            "LastSkillName": "observe_state",
        }
    )
    event = _event(lease=payload)
    runner = _Runner(result={})
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 0
    assert repository.stored is not None
    assert repository.stored.request_body_json["skillName"] == "scene_tornado"


async def test_lobby_does_not_repeat_scene_tornado_after_last_attempt() -> None:
    payload = _lease(
        available_skills=[{"skillName": "scene_tornado", "schemaVersion": "v1"}],
        hints=[
            {
                "skillName": "scene_tornado",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            }
        ],
        allowed_skill_names=[],
    )
    payload["decisionContext"]["session"].update(
        {
            "SceneId": 1,
            "SceneName": "Lobby",
            "NavigationAvailable": False,
            "SkillExecuting": False,
            "LastSkillName": "scene_tornado",
        }
    )
    event = _event(lease=payload)
    runner = _Runner(
        result={
            "errors": [],
            "selected_action": GatewayV2WaitAction(reason="wait", waitMs=1_000).model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    )
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 1
    assert repository.stored is not None
    assert repository.stored.request_body_json["action"] == "wait"


async def test_planner_temporarily_accepts_legacy_lowercase_account_id() -> None:
    payload = _lease()
    payload["decisionContext"]["session"] = {"accountId": "legacy-account-1", "status": "active"}
    context = build_gateway_v2_agent_context(_event(lease=payload))
    runner = _Runner(
        result={
            "errors": [],
            "selected_action": GatewayV2WaitAction(reason="wait", waitMs=1_000).model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    )
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
    )

    result = await planner(_claimed_for_planner(), context)

    assert result == EventProcessResult("succeeded")
    assert runner.calls == 1
    assert repository.plans == 1


async def test_planner_does_not_retry_event_without_account_id() -> None:
    payload = _lease()
    payload["decisionContext"]["session"] = {"status": "active"}
    event = _event(lease=payload)
    runner = _Runner()
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(runner=runner),
        repository=repository,
    )

    result = await planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event))

    assert result == EventProcessResult(
        "manual",
        error_stage="agent",
        error_category="missing_account_id",
    )
    assert runner.calls == 0
    assert repository.plans == 0


@pytest.mark.parametrize(
    ("runner", "timeout_seconds", "expected_category"),
    [
        (_Runner(failure=RuntimeError("provider unavailable")), 1.0, "execution_failed"),
        (_Runner(result={"errors": [], "reasoned_actions": []}), 1.0, "empty_output"),
    ],
    ids=["exception", "empty-output"],
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


async def test_agent_timeout_plans_v2_wait_when_lease_allows_wait() -> None:
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(
            runner=_BlockingRunner(),
            timeout_seconds=0.01,
        ),
        repository=repository,
    )
    claimed = _claimed_for_planner()

    result = await asyncio.wait_for(
        planner(claimed, build_gateway_v2_agent_context(claimed.event)),
        timeout=1,
    )

    assert result == EventProcessResult("succeeded")
    assert repository.plans == 1
    assert repository.stored is not None
    assert repository.stored.request_body_json == {
        "traceId": "trace-1",
        "contractVersion": "llm-gateway-http-v2",
        "sessionId": "session-1",
        "decisionId": "decision-stable-1",
        "decisionLeaseId": "lease-1",
        "stateVersion": 7,
        "controlGeneration": 3,
        "ttlMs": 30_000,
        "action": "wait",
        "waitMs": 1_000,
    }


async def test_agent_timeout_remains_retryable_when_lease_forbids_wait() -> None:
    payload = _lease(allowed_actions=["no_op"])
    event = _event(lease=payload)
    repository = _PlanningRepository()
    planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(
            runner=_BlockingRunner(),
            timeout_seconds=0.01,
        ),
        repository=repository,
    )

    result = await asyncio.wait_for(
        planner(_claimed_for_planner(event), build_gateway_v2_agent_context(event)),
        timeout=1,
    )

    assert result == EventProcessResult(
        "retryable_failed",
        error_stage="agent",
        error_category="timeout",
    )
    assert repository.plans == 0
