from __future__ import annotations

import pytest

from src.core.integration.llm_gateway_v2.activity_plan import (
    ActivityPlanProposal,
    ActivityPlanValidationError,
    create_plaza_social_plan,
    materialize_activity_plan,
    record_step_terminal,
    validate_activity_plan,
)


def test_plaza_social_plan_starts_with_arrival_and_advances_through_phases() -> None:
    plan = create_plaza_social_plan("plan-1")

    assert plan.goal_id == "plaza_social"
    assert plan.current_step_id == "arrival"
    assert plan.phase == "arrival"
    assert [step.skill_name for step in plan.steps] == [
        "scene_tornado",
        "dance_auto_schedule",
        "hot_air_balloon_auto_schedule",
        None,
    ]

    after_arrival = record_step_terminal(plan, "arrival", succeeded=True)
    assert after_arrival.current_step_id == "dance"
    assert after_arrival.phase == "activity"

    after_dance = record_step_terminal(after_arrival, "dance", succeeded=True)
    assert after_dance.current_step_id == "balloon"
    assert after_dance.phase == "transport"


def test_retryable_failure_keeps_step_pending_for_one_retry() -> None:
    plan = create_plaza_social_plan("plan-1")
    failed = record_step_terminal(plan, "arrival", succeeded=False, retryable=True)

    assert failed.current_step_id == "arrival"
    assert failed.current_step().status == "pending"
    assert failed.current_step().attempt_count == 1


def test_second_failure_skips_step_and_moves_to_next_phase() -> None:
    plan = create_plaza_social_plan("plan-1")
    first = record_step_terminal(plan, "arrival", succeeded=False, retryable=True)
    second = record_step_terminal(first, "arrival", succeeded=False, retryable=True)

    assert second.current_step_id == "dance"
    assert second.phase == "activity"
    assert second.step("arrival").status == "skipped"


def test_plan_rejects_unknown_skill_and_direct_chat_skill() -> None:
    plan = create_plaza_social_plan("plan-1").model_dump(mode="json", by_alias=True)
    plan["steps"][1]["skillName"] = "unknown_skill"

    with pytest.raises(ActivityPlanValidationError, match="skill"):
        validate_activity_plan(plan)

    plan = create_plaza_social_plan("plan-1").model_dump(mode="json", by_alias=True)
    plan["steps"][1]["skillName"] = "nearby_chat_send"

    with pytest.raises(ActivityPlanValidationError, match="chat"):
        validate_activity_plan(plan)


def test_plan_rejects_duplicate_step_ids_and_more_than_six_executable_steps() -> None:
    plan = create_plaza_social_plan("plan-1").model_dump(mode="json", by_alias=True)
    plan["steps"][1]["stepId"] = plan["steps"][0]["stepId"]

    with pytest.raises(ActivityPlanValidationError, match="stepId"):
        validate_activity_plan(plan)

    plan = create_plaza_social_plan("plan-1").model_dump(mode="json", by_alias=True)
    plan["steps"] = [
        {
            "stepId": f"step-{index}",
            "phase": "activity",
            "skillName": "dance_auto_schedule",
            "schemaVersion": "v1",
            "intent": "repeatable activity",
            "maxAttempts": 1,
            "status": "pending",
            "attemptCount": 0,
        }
        for index in range(7)
    ]
    plan["currentStepId"] = "step-0"

    with pytest.raises(ActivityPlanValidationError, match="six"):
        validate_activity_plan(plan)


def test_materialized_ai_plan_must_use_current_gateway_skill_catalog() -> None:
    proposal = ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_social",
            "goalSummary": "Complete varied plaza activities",
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
            ],
        }
    )

    with pytest.raises(ActivityPlanValidationError, match="availableSkills"):
        materialize_activity_plan(
            proposal,
            plan_id="plan-1",
            version=1,
            available_skills={
                ("scene_tornado", "v1"),
                ("dance_auto_schedule", "v1"),
            },
            lobby=True,
        )


def test_lobby_ai_plan_must_start_with_scene_tornado() -> None:
    proposal = ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_games",
            "goalSummary": "Play several plaza games",
            "steps": [
                {
                    "stepId": "dance",
                    "phase": "activity",
                    "skillName": "dance_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "dance",
                },
                {
                    "stepId": "darts",
                    "phase": "activity",
                    "skillName": "darts_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "play darts",
                },
                {
                    "stepId": "balloon",
                    "phase": "transport",
                    "skillName": "hot_air_balloon_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "ride balloon",
                },
            ],
        }
    )

    with pytest.raises(ActivityPlanValidationError, match="scene_tornado"):
        materialize_activity_plan(
            proposal,
            plan_id="plan-1",
            version=1,
            available_skills={
                ("scene_tornado", "v1"),
                ("dance_auto_schedule", "v1"),
                ("darts_auto_schedule", "v1"),
                ("hot_air_balloon_auto_schedule", "v1"),
            },
            lobby=True,
        )
