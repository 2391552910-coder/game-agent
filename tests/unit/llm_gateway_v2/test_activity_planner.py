from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.core.integration.llm_gateway_v2 import decision_service as decision_service_module
from src.core.integration.llm_gateway_v2.activity_plan import (
    ActivityPlanProposal,
    create_plaza_social_plan,
    record_step_terminal,
)
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanBinding,
    ActivityPlanContext,
    ActivityPlanSnapshot,
)
from src.core.integration.llm_gateway_v2.activity_planner import ActivityPlanCoordinator


@dataclass
class _Repository:
    snapshot: ActivityPlanSnapshot
    prepared_plans: list[Any] = field(default_factory=list)

    async def load(self, event):
        return self.snapshot

    async def prepare(self, event, context, *, proposed_plan=None):
        self.prepared_plans.append(proposed_plan)
        plan = proposed_plan or self.snapshot.plan
        assert plan is not None
        step = plan.current_step()
        return ActivityPlanContext(
            plan=plan,
            binding=ActivityPlanBinding(plan.plan_id, plan.version, step.step_id, step.phase),
            recent_actions=self.snapshot.recent_actions,
            recent_failures=self.snapshot.recent_failures,
        )


@dataclass
class _Generator:
    result: ActivityPlanProposal | Exception
    calls: int = 0

    async def generate(self, context, *, recent_actions, recent_failures):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _proposal() -> ActivityPlanProposal:
    return ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_social",
            "goalSummary": "Complete several plaza activities",
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


def _non_lobby_proposal() -> ActivityPlanProposal:
    return ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_variety",
            "goalSummary": "Complete several different plaza activities",
            "steps": [
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
                {
                    "stepId": "coffee",
                    "phase": "activity",
                    "skillName": "coffee_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "have coffee",
                },
            ],
        }
    )


def _movement_recovery_proposal() -> ActivityPlanProposal:
    return ActivityPlanProposal.model_validate(
        {
            "goalId": "movement_recovery",
            "goalSummary": "Finish the current movement lease before resuming plaza activities",
            "steps": [
                {
                    "stepId": "jump",
                    "phase": "movement",
                    "skillName": "jump",
                    "schemaVersion": "v1",
                    "intent": "Complete the movement control window",
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


@pytest.mark.asyncio
async def test_active_plan_is_resumed_without_another_model_call() -> None:
    plan = create_plaza_social_plan("plan-existing")
    repository = _Repository(ActivityPlanSnapshot(plan, (), ()))
    generator = _Generator(_proposal())
    coordinator = ActivityPlanCoordinator(repository=repository, generator=generator)

    result = await coordinator.prepare(object(), _context())

    assert result.plan.plan_id == "plan-existing"
    assert generator.calls == 0
    assert repository.prepared_plans == [None]


@pytest.mark.asyncio
async def test_missing_plan_is_generated_and_materialized_against_gateway_catalog() -> None:
    repository = _Repository(ActivityPlanSnapshot(None, (), ()))
    generator = _Generator(_proposal())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-generated",
    )

    result = await coordinator.prepare(object(), _context())

    assert result.plan.plan_id == "plan-generated"
    assert result.plan.current_step_id == "arrival"
    assert generator.calls == 1
    assert repository.prepared_plans[0].goal_id == "plaza_social"


@pytest.mark.asyncio
async def test_generator_failure_uses_safe_plan_instead_of_failing_event() -> None:
    repository = _Repository(ActivityPlanSnapshot(None, (), ()))
    generator = _Generator(TimeoutError())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-fallback",
    )

    result = await coordinator.prepare(object(), _context())

    assert result.plan.plan_id == "plan-fallback"
    assert result.plan.goal_id == "plaza_social"
    assert result.plan.current_step_id == "arrival"


@pytest.mark.asyncio
async def test_safe_fallback_uses_only_the_current_gateway_catalog() -> None:
    repository = _Repository(ActivityPlanSnapshot(None, (), ()))
    generator = _Generator(TimeoutError())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-dynamic-fallback",
    )
    catalog = [
        "coffee_auto_schedule",
        "darts_auto_schedule",
        "shooting_auto_schedule",
        "paper_plane_auto_schedule",
    ]

    result = await coordinator.prepare(
        object(),
        _context(skills=catalog, lobby=False),
    )

    assert result is not None
    executable = [step.skill_name for step in result.plan.steps if step.skill_name is not None]
    assert 3 <= len(executable) <= 6
    assert set(executable).issubset(set(catalog))
    assert "scene_tornado" not in executable
    assert "dance_auto_schedule" not in executable
    assert "hot_air_balloon_auto_schedule" not in executable


@pytest.mark.asyncio
async def test_safe_fallback_defers_when_gateway_catalog_cannot_form_a_valid_plan() -> None:
    repository = _Repository(ActivityPlanSnapshot(None, (), ()))
    generator = _Generator(TimeoutError())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-deferred",
    )

    result = await coordinator.prepare(
        object(),
        _context(skills=["observe_state", "jump"], lobby=False),
    )

    assert result is None
    assert repository.prepared_plans == []


@pytest.mark.asyncio
async def test_unavailable_active_step_is_replanned_with_next_version() -> None:
    existing = create_plaza_social_plan("plan-existing")
    repository = _Repository(ActivityPlanSnapshot(existing, (), (), version=1))
    generator = _Generator(_non_lobby_proposal())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-replanned",
    )

    result = await coordinator.prepare(
        object(),
        _context(
            skills=[
                "dance_auto_schedule",
                "hot_air_balloon_auto_schedule",
                "coffee_auto_schedule",
            ],
            lobby=False,
        ),
    )

    assert generator.calls == 1
    assert result.plan.plan_id == "plan-replanned"
    assert result.plan.version == 2
    assert result.plan.current_step_id == "dance"


@pytest.mark.asyncio
async def test_active_step_outside_current_lease_is_replanned_with_authorized_first_step() -> None:
    existing = record_step_terminal(
        create_plaza_social_plan("plan-existing"),
        "arrival",
        succeeded=True,
    )
    repository = _Repository(ActivityPlanSnapshot(existing, (), (), version=1))
    generator = _Generator(_movement_recovery_proposal())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-lease-replanned",
        step_authorizer=decision_service_module.gateway_v2_activity_skill_is_permitted,
    )

    result = await coordinator.prepare(
        object(),
        _context(
            skills=[
                "jump",
                "dance_auto_schedule",
                "hot_air_balloon_auto_schedule",
            ],
            lobby=False,
            lease_kind="movement_control",
            allowed_skill_names=["jump"],
        ),
    )

    assert result is not None
    assert generator.calls == 1
    assert result.plan.plan_id == "plan-lease-replanned"
    assert result.plan.version == 2
    assert result.plan.current_step_id == "jump"


def _context(
    *,
    skills: list[str] | None = None,
    lobby: bool = True,
    lease_kind: str = "observation",
    allowed_skill_names: list[str] | None = None,
):
    from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
    from src.core.integration.llm_gateway_v2.decision_service import build_gateway_v2_agent_context

    skills = skills or [
        "scene_tornado",
        "dance_auto_schedule",
        "hot_air_balloon_auto_schedule",
    ]
    event = parse_gateway_v2_event(
        {
            "eventId": "event-1",
            "eventType": "session_started",
            "sessionId": "session-1",
            "controlGeneration": 1,
            "eventSequence": 1,
            "stateVersion": 1,
            "decisionLeaseId": "lease-1",
            "occurredAtMs": 1,
            "payload": {
                "reason": "decision_requested",
                "lease": {
                    "sessionId": "session-1",
                    "controlGeneration": 1,
                    "decisionLeaseId": "lease-1",
                    "stateVersion": 1,
                    "leaseKind": lease_kind,
                    "allowedActions": ["call_skill", "wait"],
                    "allowedSkillName": None,
                    "allowedSkillNames": skills if allowed_skill_names is None else allowed_skill_names,
                    "parentSkillName": None,
                },
                "decisionContext": {
                    "session": {
                        "AccountId": "account-1",
                        "SceneId": 1 if lobby else 2,
                        "SceneName": "Lobby" if lobby else "Plaza",
                        "NavigationAvailable": not lobby,
                    },
                    "availableSkills": [
                        {
                            "SkillName": skill,
                            "SchemaVersion": "v1",
                            "RequireRunning": True,
                            "CooldownMs": 0,
                        }
                        for skill in skills
                    ],
                    "skillArgumentHints": [
                        {
                            "skillName": skill,
                            "schemaVersion": "v1",
                            "argumentStatus": "ready",
                            "suggestedArgs": {},
                            "allowedArgs": [],
                            "missingArgs": [],
                            "warnings": [],
                            "nextSteps": [],
                        }
                        for skill in skills
                    ],
                    "lastSkillResult": None,
                },
            },
        }
    )
    return build_gateway_v2_agent_context(event)
