from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.core.integration.llm_gateway_v2.plan_runtime import (
    CANONICAL_NON_CHAT_SKILLS,
    SEAT_COFFEE_SEQUENCE,
    PlanExecutionContext,
    PlanStateStore,
    PlanValidationError,
    prepare_next_action,
    record_step_success,
    record_step_terminal,
    validate_execution_plan,
)


def _plan(*, expected_skills: list[str] | None = None) -> dict:
    return {
        "planId": "plan-seat-coffee-1",
        "sessionId": "session-plan-1",
        "gatewayId": "gateway-plan-1",
        "sceneId": "scene-square-1",
        "expectedSkills": list(CANONICAL_NON_CHAT_SKILLS if expected_skills is None else expected_skills),
        "steps": [
            {
                "stepId": "sit",
                "skillName": "seat_sit",
                "arguments": {"sceneId": "scene-square-1", "chairId": "chair-1"},
            },
            {
                "stepId": "coffee",
                "skillName": "coffee_auto_schedule",
                "arguments": {"sceneId": "scene-square-1", "coffeeName": "latte"},
            },
            {
                "stepId": "get-out",
                "skillName": "seat_get_out",
                "arguments": {"sceneId": "scene-square-1", "chairId": "chair-1"},
            },
        ],
    }


def test_plan_requires_exact_canonical_21_before_runtime_events() -> None:
    plan = validate_execution_plan(_plan())

    assert plan.expected_skills == CANONICAL_NON_CHAT_SKILLS
    assert tuple(step.skill_name for step in plan.steps) == SEAT_COFFEE_SEQUENCE


@pytest.mark.parametrize(
    ("step_index", "argument_name"),
    [(0, "chairId"), (1, "coffeeName"), (2, "chairId")],
)
def test_plan_requires_sequence_action_arguments(step_index: int, argument_name: str) -> None:
    value = _plan()
    value["steps"][step_index]["arguments"].pop(argument_name)

    with pytest.raises(PlanValidationError):
        validate_execution_plan(value)


@pytest.mark.parametrize(
    "expected_skills",
    [
        list(CANONICAL_NON_CHAT_SKILLS[:-1]),
        [*CANONICAL_NON_CHAT_SKILLS, "unexpected_skill"],
        [*CANONICAL_NON_CHAT_SKILLS[:-1], CANONICAL_NON_CHAT_SKILLS[-2]],
        [skill for skill in CANONICAL_NON_CHAT_SKILLS if skill != "coffee_auto_schedule"],
    ],
)
def test_plan_rejects_missing_duplicate_extra_or_undeclared_skill(expected_skills: list[str]) -> None:
    with pytest.raises(PlanValidationError):
        validate_execution_plan(_plan(expected_skills=expected_skills))


def test_state_initialization_does_not_store_complete_action_arguments(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    state_path = tmp_path / "plan-state.json"

    state = PlanStateStore(state_path).load_or_initialize(plan)
    stored = json.loads(state_path.read_text(encoding="utf-8"))

    assert state.plan_id == plan.plan_id
    assert stored["currentStepId"] == "sit"
    assert "arguments" not in json.dumps(stored)
    assert "action" not in stored


def test_invalid_existing_state_is_not_overwritten(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    state_path = tmp_path / "plan-state.json"
    original = '{"schemaVersion":"wrong","planId":"other-plan"}\n'
    state_path.write_text(original, encoding="utf-8")

    with pytest.raises(PlanValidationError):
        PlanStateStore(state_path).load_or_initialize(plan)

    assert state_path.read_text(encoding="utf-8") == original


def test_corrupt_existing_state_is_not_overwritten(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    state_path = tmp_path / "plan-state.json"
    original = '{"schemaVersion":'
    state_path.write_text(original, encoding="utf-8")

    with pytest.raises(PlanValidationError):
        PlanStateStore(state_path).load_or_initialize(plan)

    assert state_path.read_text(encoding="utf-8") == original


def test_same_plan_id_with_changed_content_cannot_resume(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    state_path = tmp_path / "plan-state.json"
    store = PlanStateStore(state_path)
    store.load_or_initialize(plan)

    changed = _plan()
    changed["steps"][0]["arguments"]["chairId"] = "chair-2"
    changed["steps"][2]["arguments"]["chairId"] = "chair-2"
    changed_plan = validate_execution_plan(changed)

    with pytest.raises(PlanValidationError, match="digest"):
        store.load_or_initialize(changed_plan)


def test_pending_recovery_rebuilds_action_from_current_plan_and_rechecks_context(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    store = PlanStateStore(tmp_path / "plan-state.json")
    state = store.load_or_initialize(plan)

    action = prepare_next_action(
        plan,
        state,
        PlanExecutionContext(
            session_id="session-plan-1",
            gateway_id="gateway-plan-1",
            scene_id="scene-square-1",
            decision_lease_id="lease-1",
            lease_expires_at_ms=2_000,
            now_ms=1_000,
            seat_state="standing",
        ),
    )

    assert action.skill_name == "seat_sit"
    assert action.arguments == {"sceneId": "scene-square-1", "chairId": "chair-1"}
    with pytest.raises(TypeError):
        action.arguments["chairId"] = "chair-2"


def test_seat_coffee_sequence_requires_successful_predecessor_and_seated_state(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    store = PlanStateStore(tmp_path / "plan-state.json")
    state = store.load_or_initialize(plan)
    context = PlanExecutionContext(
        session_id="session-plan-1",
        gateway_id="gateway-plan-1",
        scene_id="scene-square-1",
        decision_lease_id="lease-1",
        lease_expires_at_ms=2_000,
        now_ms=1_000,
        seat_state="standing",
    )

    inconsistent_coffee_state = replace(state, current_step_id="coffee")
    with pytest.raises(PlanValidationError, match="seat_sit"):
        prepare_next_action(plan, inconsistent_coffee_state, context)

    state = record_step_success(state, "sit")
    seated_context = context.__class__(**{**context.__dict__, "seat_state": "seated"})
    coffee = prepare_next_action(plan, state, seated_context)

    assert coffee.skill_name == "coffee_auto_schedule"
    assert coffee.arguments["coffeeName"] == "latte"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_id", "other-session", "session"),
        ("gateway_id", "other-gateway", "gateway"),
        ("scene_id", "other-scene", "scene"),
        ("decision_lease_id", "", "lease"),
        ("lease_expires_at_ms", 1_000, "lease"),
    ],
)
def test_pending_recovery_rechecks_identity_and_lease(
    tmp_path: Path,
    field: str,
    value: str | int,
    message: str,
) -> None:
    plan = validate_execution_plan(_plan())
    state = PlanStateStore(tmp_path / "plan-state.json").load_or_initialize(plan)
    context = PlanExecutionContext(
        session_id="session-plan-1",
        gateway_id="gateway-plan-1",
        scene_id="scene-square-1",
        decision_lease_id="lease-1",
        lease_expires_at_ms=2_000,
        now_ms=1_000,
        seat_state="standing",
    )
    invalid_context = replace(context, **{field: value})

    with pytest.raises(PlanValidationError, match=message):
        prepare_next_action(plan, state, invalid_context)


def test_coffee_terminal_failure_still_advances_to_get_out(tmp_path: Path) -> None:
    plan = validate_execution_plan(_plan())
    state = PlanStateStore(tmp_path / "plan-state.json").load_or_initialize(plan)
    state = record_step_success(state, "sit")
    state = record_step_terminal(state, "coffee", succeeded=False)
    context = PlanExecutionContext(
        session_id="session-plan-1",
        gateway_id="gateway-plan-1",
        scene_id="scene-square-1",
        decision_lease_id="lease-2",
        lease_expires_at_ms=3_000,
        now_ms=2_000,
        seat_state="seated",
    )

    action = prepare_next_action(plan, state, context)

    assert action.skill_name == "seat_get_out"
    assert action.arguments["chairId"] == "chair-1"
