from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda

from src.core.integration.llm_gateway_v2 import activity_planner as activity_planner_module
from src.core.integration.llm_gateway_v2 import decision_service as decision_service_module
from src.core.integration.llm_gateway_v2.activity_capacity import ActivityCapacitySnapshot
from src.core.integration.llm_gateway_v2.activity_plan import (
    ActivityPlan,
    ActivityPlanProposal,
    create_plaza_social_plan,
    record_step_terminal,
    validate_activity_plan,
)
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanBinding,
    ActivityPlanContext,
    ActivityPlanSnapshot,
)
from src.core.integration.llm_gateway_v2.activity_planner import (
    ActivityPlanCoordinator,
    GatewayV2ActivityPlanGenerator,
    diversify_activity_plan_proposal,
)
from src.core.integration.llm_gateway_v2.capacity import AgentCapacityExceededError, AgentCapacityLimiter
from src.core.integration.llm_gateway_v2.scene_catalog import (
    SceneCatalog,
    SceneCoordinates,
    SceneTarget,
    load_default_scene_catalog,
)
from src.core.integration.llm_gateway_v2.token_usage import gateway_v2_token_usage_callback


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


@pytest.mark.asyncio
async def test_model_plan_generation_passes_v2_call_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _StructuredOutputStub:
        def with_structured_output(self, schema: Any, *, method: str) -> RunnableLambda:
            assert schema is ActivityPlanProposal
            assert method == "json_mode"

            async def invoke(_: Any, config: dict[str, Any]) -> ActivityPlanProposal:
                captured["config"] = config
                return _proposal()

            return RunnableLambda(invoke)

    async def fake_get_llm(*, model_type: str) -> _StructuredOutputStub:
        assert model_type == "default"
        return _StructuredOutputStub()

    monkeypatch.setattr(activity_planner_module, "get_llm", fake_get_llm)
    monkeypatch.setattr(
        activity_planner_module,
        "llm_call_config",
        lambda **_: {
            "callbacks": [gateway_v2_token_usage_callback],
            "metadata": {
                "token_scope_marker": "activity-plan",
                "flow": "gateway_v2",
                "node": "activity_plan_generation",
                "model_type": "default",
            },
        },
        raising=False,
    )

    result = await GatewayV2ActivityPlanGenerator().generate(
        _context(),
        recent_actions=(),
        recent_failures=(),
    )

    assert result.goal_id == "plaza_social"
    assert gateway_v2_token_usage_callback in captured["config"]["callbacks"].handlers
    assert captured["config"]["metadata"] == {
        "token_scope_marker": "activity-plan",
        "flow": "gateway_v2",
        "node": "activity_plan_generation",
        "model_type": "default",
    }


@pytest.mark.asyncio
async def test_activity_plan_generation_uses_bounded_agent_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = AgentCapacityLimiter(limit=1, acquire_timeout_seconds=0.01)
    generator = GatewayV2ActivityPlanGenerator(capacity_limiter=limiter)

    async def fail_if_model_is_called(*, model_type: str) -> Any:
        raise AssertionError(f"model should not be called while capacity is full: {model_type}")

    monkeypatch.setattr(activity_planner_module, "get_llm", fail_if_model_is_called)

    async with limiter.slot():
        with pytest.raises(AgentCapacityExceededError):
            await generator.generate(_context(), recent_actions=(), recent_failures=())


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


@pytest.mark.asyncio
async def test_ten_roles_use_scene_targets_and_stable_role_rotation() -> None:
    scene_catalog = load_default_scene_catalog()

    plans = []
    wire_decisions: list[dict[str, Any]] = []
    for index in range(10):
        repository = _Repository(ActivityPlanSnapshot(None, (), ()))
        coordinator = ActivityPlanCoordinator(
            repository=repository,
            generator=_Generator(_proposal()),
            plan_id_factory=lambda index=index: f"plan-role-{index}",
            scene_catalog=scene_catalog,
            step_authorizer=lambda context, skill_name, schema_version: True,
        )
        context = _context(
            skills=[
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
                "move_to",
            ],
            lobby=False,
            account_id=f"role-{index}",
            scene_id=7,
        )
        result = await coordinator.prepare(object(), context)
        assert result is not None
        plans.append(result.plan)
        action = decision_service_module._planned_activity_action(
            context,
            result,
            scene_catalog=scene_catalog,
        )
        assert action is not None
        wire_decisions.append(
            decision_service_module.freeze_gateway_v2_decision(
                f"decision-role-{index}",
                f"trace-role-{index}",
                context,
                action,
            ).body_json
        )

    executable_orders = [tuple(step.skill_name for step in plan.steps if step.skill_name is not None) for plan in plans]
    move_steps = [step for plan in plans for step in plan.steps if step.skill_name == "move_to"]

    assert len(set(executable_orders)) == 10
    assert all(step.scene_target_id is not None for step in move_steps)
    assert len({step.scene_target_id for step in move_steps}) == 10
    assert len({body["skillName"] for body in wire_decisions}) == 10


def test_full_activities_are_removed_from_plan_diversification() -> None:
    context = _context(
        skills=[
            "dance_auto_schedule",
            "darts_auto_schedule",
            "shooting_auto_schedule",
            "coffee_auto_schedule",
            "move_to",
        ],
        lobby=False,
        account_id="role-capacity-1",
        scene_id=8,
    )
    capacity = ActivityCapacitySnapshot(
        scene_id=8,
        active_by_skill={
            "dance_auto_schedule": 33,
            "darts_auto_schedule": 16,
            "shooting_auto_schedule": 25,
        },
    )

    proposal = diversify_activity_plan_proposal(
        _non_lobby_proposal(),
        context,
        scene_catalog=load_default_scene_catalog(),
        plan_version=1,
        recent_actions=(),
        step_authorizer=lambda context, skill_name, schema_version: True,
        capacity_snapshot=capacity,
    )

    assert "dance_auto_schedule" not in {step.skill_name for step in proposal.steps}
    assert "move_to" in {step.skill_name for step in proposal.steps}


@pytest.mark.asyncio
async def test_new_non_lobby_plan_avoids_most_recent_successful_skill() -> None:
    recent_skill = "hot_air_balloon_auto_schedule"
    recent_actions = (
        {
            "action": "call_skill",
            "request_body_json": {
                "action": "call_skill",
                "skillName": recent_skill,
            },
            "skill_name": recent_skill,
            "skill_status": "succeeded",
        },
    )
    repository = _Repository(ActivityPlanSnapshot(None, recent_actions, (), version=1))
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=_Generator(_non_lobby_proposal()),
        plan_id_factory=lambda: "plan-after-balloon",
        scene_catalog=load_default_scene_catalog(),
        step_authorizer=lambda context, skill_name, schema_version: True,
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
            account_id="role-0",
            scene_id=7,
        ),
    )

    assert result is not None
    executable = [step.skill_name for step in result.plan.steps if step.skill_name is not None]
    assert executable[0] != recent_skill
    assert set(executable) == {
        "dance_auto_schedule",
        "hot_air_balloon_auto_schedule",
        "coffee_auto_schedule",
    }


@pytest.mark.asyncio
async def test_lobby_arrival_remains_first_after_recent_scene_tornado() -> None:
    recent_actions = (
        {
            "action": "call_skill",
            "request_body_json": {
                "action": "call_skill",
                "skillName": "scene_tornado",
            },
            "skill_name": "scene_tornado",
            "skill_status": "succeeded",
        },
    )
    repository = _Repository(ActivityPlanSnapshot(None, recent_actions, ()))
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=_Generator(_proposal()),
        plan_id_factory=lambda: "plan-lobby-arrival",
        scene_catalog=load_default_scene_catalog(),
        step_authorizer=lambda context, skill_name, schema_version: True,
    )

    result = await coordinator.prepare(
        object(),
        _context(
            skills=[
                "scene_tornado",
                "dance_auto_schedule",
                "hot_air_balloon_auto_schedule",
                "coffee_auto_schedule",
            ],
            lobby=True,
            account_id="role-0",
            scene_id=1,
        ),
    )

    assert result is not None
    assert result.plan.current_step().skill_name == "scene_tornado"


@pytest.mark.asyncio
async def test_scene_change_replans_targetless_move_step_with_current_scene_target() -> None:
    existing = ActivityPlan.model_validate(
        {
            "planId": "plan-created-in-lobby",
            "goalId": "plaza_social",
            "goalSummary": "Continue the plan after entering the plaza",
            "phase": "movement",
            "status": "active",
            "version": 1,
            "currentStepId": "wander-after-arrival",
            "steps": [
                {
                    "stepId": "wander-after-arrival",
                    "phase": "movement",
                    "skillName": "move_to",
                    "schemaVersion": "v1",
                    "intent": "Walk to a plaza point",
                },
                {
                    "stepId": "dance",
                    "phase": "activity",
                    "skillName": "dance_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "dance",
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
    repository = _Repository(ActivityPlanSnapshot(existing, (), (), version=1))
    generator = _Generator(_non_lobby_proposal())
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
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        plan_id_factory=lambda: "plan-replanned-in-plaza",
        scene_catalog=scene_catalog,
        step_authorizer=lambda context, skill_name, schema_version: True,
    )

    result = await coordinator.prepare(
        object(),
        _context(
            skills=[
                "move_to",
                "dance_auto_schedule",
                "hot_air_balloon_auto_schedule",
                "coffee_auto_schedule",
            ],
            lobby=False,
            account_id="role-7",
            scene_id=7,
        ),
    )

    assert result is not None
    assert generator.calls == 1
    assert result.plan.plan_id == "plan-replanned-in-plaza"
    assert result.plan.version == 2
    move_steps = [step for step in result.plan.steps if step.skill_name == "move_to"]
    assert move_steps
    assert {step.scene_target_id for step in move_steps} == {"scene:7:activity:wish_board:458"}


@pytest.mark.asyncio
@pytest.mark.parametrize("generator_result", [_non_lobby_proposal(), TimeoutError()])
async def test_scene_without_trusted_targets_omits_move_to_without_deferring_plan(
    generator_result: ActivityPlanProposal | Exception,
) -> None:
    repository = _Repository(ActivityPlanSnapshot(None, (), ()))
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
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=_Generator(generator_result),
        plan_id_factory=lambda: "plan-scene-without-targets",
        scene_catalog=scene_catalog,
        step_authorizer=lambda context, skill_name, schema_version: True,
    )

    result = await coordinator.prepare(
        object(),
        _context(
            skills=[
                "dance_auto_schedule",
                "hot_air_balloon_auto_schedule",
                "coffee_auto_schedule",
                "darts_auto_schedule",
                "move_to",
            ],
            lobby=False,
            account_id="role-42",
            scene_id=42,
        ),
    )

    assert result is not None
    executable = [step.skill_name for step in result.plan.steps if step.skill_name is not None]
    assert len(executable) >= 3
    assert "move_to" not in executable


@pytest.mark.asyncio
async def test_non_lobby_plan_does_not_repeat_scene_tornado() -> None:
    repository = _Repository(ActivityPlanSnapshot(None, (), ()))
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
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=_Generator(_proposal()),
        plan_id_factory=lambda: "plan-plaza",
        scene_catalog=scene_catalog,
        step_authorizer=lambda context, skill_name, schema_version: True,
    )

    result = await coordinator.prepare(
        object(),
        _context(
            skills=[
                "scene_tornado",
                "dance_auto_schedule",
                "hot_air_balloon_auto_schedule",
                "coffee_auto_schedule",
            ],
            lobby=False,
            scene_id=7,
        ),
    )

    assert result is not None
    assert all(step.skill_name != "scene_tornado" for step in result.plan.steps)


@pytest.mark.asyncio
async def test_new_lease_retargets_existing_move_step_without_calling_model() -> None:
    existing = validate_activity_plan(
        {
            "planId": "plan-existing",
            "goalId": "plaza_explore",
            "goalSummary": "Explore the current scene",
            "phase": "movement",
            "status": "active",
            "version": 1,
            "currentStepId": "move",
            "steps": [
                {
                    "stepId": "move",
                    "phase": "movement",
                    "skillName": "move_to",
                    "schemaVersion": "v1",
                    "sceneTargetId": "scene:7:navigation:1",
                    "intent": "Walk to a trusted scene point",
                },
                {
                    "stepId": "dance",
                    "phase": "activity",
                    "skillName": "dance_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "Dance at the plaza",
                },
                {
                    "stepId": "coffee",
                    "phase": "activity",
                    "skillName": "coffee_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "Have coffee",
                },
            ],
        }
    )
    repository = _Repository(
        ActivityPlanSnapshot(existing, (), (), version=1, last_event_sequence=1)
    )
    scene_catalog = SceneCatalog(
        [
            SceneTarget(
                target_id=f"scene:7:navigation:{index}",
                scene_id=7,
                scene_name="CJ_guangchang",
                kind="navigation",
                activity="wander",
                point_key=str(index),
                coordinates=SceneCoordinates(float(index), 0.0, 2.0),
                source_path="test-scene",
            )
            for index in (1, 2)
        ]
    )
    generator = _Generator(_proposal())
    coordinator = ActivityPlanCoordinator(
        repository=repository,
        generator=generator,
        scene_catalog=scene_catalog,
        step_authorizer=lambda context, skill_name, schema_version: True,
    )

    result = await coordinator.prepare(
        SimpleNamespace(event_sequence=2),
        _context(
            skills=["move_to", "dance_auto_schedule", "coffee_auto_schedule"],
            lobby=False,
            scene_id=7,
        ),
    )

    assert result is not None
    assert generator.calls == 0
    assert result.plan.version == 2
    assert result.plan.current_step().scene_target_id == "scene:7:navigation:2"


def _context(
    *,
    skills: list[str] | None = None,
    lobby: bool = True,
    lease_kind: str = "observation",
    allowed_skill_names: list[str] | None = None,
    account_id: str = "account-1",
    scene_id: int | None = None,
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
                        "AccountId": account_id,
                        "SceneId": (1 if lobby else 2) if scene_id is None else scene_id,
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
                            "suggestedArgs": (
                                {"boardName": "wish-board-1", "wish": "Have a good day"}
                                if skill == "wish_board_auto_schedule"
                                else {"coffeeName": "latte"}
                                if skill == "coffee_auto_schedule"
                                else {"sceneId": 7, "chairId": 1}
                                if skill in {"seat_sit", "seat_get_out"}
                                else {}
                            ),
                            "allowedArgs": (
                                [
                                    {"path": "target.x"},
                                    {"path": "target.y"},
                                    {"path": "target.z"},
                                ]
                                if skill == "move_to"
                                else [
                                    {"path": "planeName"},
                                    {"path": "useTimeMs"},
                                    {"path": "isComplete"},
                                ]
                                if skill == "paper_plane_auto_schedule"
                                else [
                                    {"path": "score"},
                                    {"path": "darts"},
                                    {"path": "allowPurchaseWhenInsufficient"},
                                ]
                                if skill == "darts_auto_schedule"
                                else [
                                    {"path": "distance"},
                                    {"path": "weapon"},
                                    {"path": "posture"},
                                    {"path": "score"},
                                ]
                                if skill == "shooting_auto_schedule"
                                else [{"path": "score"}]
                                if skill == "dance_auto_schedule"
                                else [
                                    {"path": "boardName"},
                                    {"path": "wish"},
                                ]
                                if skill == "wish_board_auto_schedule"
                                else [{"path": "coffeeName"}]
                                if skill == "coffee_auto_schedule"
                                else [
                                    {"path": "sceneId"},
                                    {"path": "chairId"},
                                ]
                                if skill in {"seat_sit", "seat_get_out"}
                                else []
                            ),
                            "missingArgs": (
                                [
                                    {"path": "target.x"},
                                    {"path": "target.y"},
                                    {"path": "target.z"},
                                ]
                                if skill == "move_to"
                                else [
                                    {"path": "planeName"},
                                    {"path": "useTimeMs"},
                                    {"path": "isComplete"},
                                ]
                                if skill == "paper_plane_auto_schedule"
                                else [
                                    {"path": "score"},
                                    {"path": "darts"},
                                    {"path": "allowPurchaseWhenInsufficient"},
                                ]
                                if skill == "darts_auto_schedule"
                                else [
                                    {"path": "distance"},
                                    {"path": "weapon"},
                                    {"path": "posture"},
                                    {"path": "score"},
                                ]
                                if skill == "shooting_auto_schedule"
                                else [{"path": "score"}]
                                if skill == "dance_auto_schedule"
                                else [
                                    {"path": "boardName"},
                                    {"path": "wish"},
                                ]
                                if skill == "wish_board_auto_schedule"
                                else [{"path": "coffeeName"}]
                                if skill == "coffee_auto_schedule"
                                else [
                                    {"path": "sceneId"},
                                    {"path": "chairId"},
                                ]
                                if skill in {"seat_sit", "seat_get_out"}
                                else []
                            ),
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
