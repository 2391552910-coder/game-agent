from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

CANONICAL_NON_CHAT_SKILLS: tuple[str, ...] = (
    "observe_state",
    "move_to",
    "stop_move",
    "jump",
    "play_action",
    "scene_tornado",
    "sign_in",
    "shooting_auto_schedule",
    "darts_auto_schedule",
    "dance_auto_schedule",
    "draw_lots_auto_schedule",
    "wish_board_auto_schedule",
    "paper_plane_auto_schedule",
    "coffee_auto_schedule",
    "seat_sit",
    "seat_get_out",
    "hot_air_balloon_auto_schedule",
    "hot_air_balloon_exit",
    "helicopter_auto_schedule",
    "helicopter_exit",
    "elevator_auto_schedule",
)

NonEmptyString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
PlanStatus = Literal["active", "completed", "paused", "abandoned"]
StepStatus = Literal["pending", "started", "succeeded", "failed", "skipped"]

# Gateway may mark a failure retryable for transport-level convergence, while
# the activity itself is already known to be unavailable or too long-running.
# Retrying those activities immediately creates the pressure-test retry storms.
_ACTIVITY_RETRY_SUPPRESSION_REASONS = frozenset(
    {
        "no_available_dart_pos",
        "no_available_seat",
        "no_available_chair",
        "no_available_vehicle",
        "ttl_expired",
        "upstream_timeout",
    }
)


class ActivityPlanValidationError(ValueError):
    pass


def should_retry_activity_failure(
    skill_name: str,
    reason: str,
    *,
    retryable: bool,
) -> bool:
    """Return whether the current plan step may be retried immediately.

    The terminal row still records the Gateway-provided retryable flag. This
    decision only controls activity-plan advancement, so resource exhaustion
    and long-running activity timeouts can move to the next activity without
    losing the original failure evidence.
    """

    del skill_name
    return retryable and reason not in _ACTIVITY_RETRY_SUPPRESSION_REASONS


class _ActivityModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ActivityPlanStep(_ActivityModel):
    step_id: NonEmptyString = Field(alias="stepId", max_length=128)
    phase: NonEmptyString = Field(max_length=64)
    skill_name: NonEmptyString | None = Field(default=None, alias="skillName", max_length=128)
    schema_version: NonEmptyString | None = Field(default=None, alias="schemaVersion", max_length=128)
    scene_target_id: NonEmptyString | None = Field(
        default=None,
        alias="sceneTargetId",
        max_length=256,
    )
    intent: NonEmptyString
    max_attempts: int = Field(default=2, alias="maxAttempts", ge=1, le=2)
    status: StepStatus = "pending"
    attempt_count: int = Field(default=0, alias="attemptCount", ge=0, le=2)

    @model_validator(mode="after")
    def validate_action_identity(self) -> ActivityPlanStep:
        if (self.skill_name is None) != (self.schema_version is None):
            raise ValueError("skillName and schemaVersion must be supplied together")
        if self.skill_name == "nearby_chat_send":
            raise ValueError("direct chat skill is not an activity plan step")
        if self.scene_target_id is not None and self.skill_name != "move_to":
            raise ValueError("sceneTargetId is only valid for move_to")
        if self.skill_name is not None and self.skill_name not in CANONICAL_NON_CHAT_SKILLS:
            raise ValueError("skillName is not in the canonical non-chat catalog")
        if self.skill_name is not None and self.schema_version != "v1":
            raise ValueError("activity skill schemaVersion must be v1")
        if self.skill_name is None and self.phase != "social":
            raise ValueError("only a social opportunity step may omit skillName")
        if self.attempt_count > self.max_attempts:
            raise ValueError("attemptCount must not exceed maxAttempts")
        return self


class ActivityPlanProposalStep(_ActivityModel):
    step_id: NonEmptyString = Field(alias="stepId", max_length=128)
    phase: NonEmptyString = Field(max_length=64)
    skill_name: NonEmptyString | None = Field(default=None, alias="skillName", max_length=128)
    schema_version: NonEmptyString | None = Field(default=None, alias="schemaVersion", max_length=128)
    scene_target_id: NonEmptyString | None = Field(
        default=None,
        alias="sceneTargetId",
        max_length=256,
    )
    intent: NonEmptyString

    @model_validator(mode="after")
    def validate_action_identity(self) -> ActivityPlanProposalStep:
        if (self.skill_name is None) != (self.schema_version is None):
            raise ValueError("skillName and schemaVersion must be supplied together")
        if self.skill_name == "nearby_chat_send":
            raise ValueError("direct chat skill is not an activity plan step")
        if self.scene_target_id is not None and self.skill_name != "move_to":
            raise ValueError("sceneTargetId is only valid for move_to")
        if self.skill_name is not None and self.skill_name not in CANONICAL_NON_CHAT_SKILLS:
            raise ValueError("skillName is not in the canonical non-chat catalog")
        if self.skill_name is not None and self.schema_version != "v1":
            raise ValueError("activity skill schemaVersion must be v1")
        if self.skill_name is None and self.phase != "social":
            raise ValueError("only a social opportunity step may omit skillName")
        return self


def _validate_social_opportunity_layout(
    steps: tuple[ActivityPlanProposalStep | ActivityPlanStep, ...],
) -> None:
    social_indexes = [
        index for index, step in enumerate(steps) if step.skill_name is None
    ]
    if len(social_indexes) > 1:
        raise ValueError("activity plan may contain at most one social opportunity step")
    if social_indexes and social_indexes[0] != len(steps) - 1:
        raise ValueError("social opportunity step must be the final plan step")


class ActivityPlanProposal(_ActivityModel):
    goal_id: NonEmptyString = Field(alias="goalId", max_length=128)
    goal_summary: NonEmptyString = Field(alias="goalSummary")
    steps: tuple[ActivityPlanProposalStep, ...]

    @model_validator(mode="after")
    def validate_steps(self) -> ActivityPlanProposal:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("stepId values must be unique")
        _validate_social_opportunity_layout(self.steps)
        executable_count = sum(step.skill_name is not None for step in self.steps)
        if executable_count < 3:
            raise ValueError("activity plan requires at least three executable steps")
        if executable_count > 6:
            raise ValueError("activity plan cannot contain more than six executable steps")
        if len(self.steps) > 7:
            raise ValueError("activity plan contains too many total steps")
        return self


class ActivityPlan(_ActivityModel):
    plan_id: NonEmptyString = Field(alias="planId", max_length=128)
    goal_id: NonEmptyString = Field(alias="goalId", max_length=128)
    goal_summary: NonEmptyString = Field(alias="goalSummary")
    phase: NonEmptyString = Field(max_length=64)
    status: PlanStatus = "active"
    version: int = Field(default=1, ge=1)
    current_step_id: NonEmptyString | None = Field(alias="currentStepId", max_length=128)
    steps: tuple[ActivityPlanStep, ...]

    @model_validator(mode="after")
    def validate_plan_state(self) -> ActivityPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("stepId values must be unique")
        _validate_social_opportunity_layout(self.steps)
        executable_count = sum(step.skill_name is not None for step in self.steps)
        if executable_count < 3:
            raise ValueError("activity plan requires at least three executable steps")
        if executable_count > 6:
            raise ValueError("activity plan cannot contain more than six executable steps")
        if len(self.steps) > 7:
            raise ValueError("activity plan contains too many total steps")
        if self.current_step_id is not None and self.current_step_id not in set(step_ids):
            raise ValueError("currentStepId must reference a plan step")
        if self.status == "completed" and self.current_step_id is not None:
            raise ValueError("completed activity plan cannot have a current step")
        if self.status == "active" and self.current_step_id is None:
            raise ValueError("active activity plan requires a current step")
        if self.current_step_id is not None and self.step(self.current_step_id).phase != self.phase:
            raise ValueError("phase must match the current step")
        return self

    def step(self, step_id: str) -> ActivityPlanStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise ActivityPlanValidationError("activity plan step does not exist")

    def current_step(self) -> ActivityPlanStep:
        if self.current_step_id is None:
            raise ActivityPlanValidationError("activity plan is complete")
        return self.step(self.current_step_id)


def validate_activity_plan(value: object) -> ActivityPlan:
    try:
        return ActivityPlan.model_validate(value)
    except (ValidationError, ValueError) as error:
        raise ActivityPlanValidationError(str(error)) from error


def create_plaza_social_plan(plan_id: str, *, version: int = 1) -> ActivityPlan:
    return ActivityPlan.model_validate(
        {
            "planId": plan_id,
            "goalId": "plaza_social",
            "goalSummary": "Enter the plaza, complete varied activities, and remain open to nearby social events",
            "phase": "arrival",
            "status": "active",
            "version": version,
            "currentStepId": "arrival",
            "steps": [
                {
                    "stepId": "arrival",
                    "phase": "arrival",
                    "skillName": "scene_tornado",
                    "schemaVersion": "v1",
                    "intent": "Move from the initial room to the plaza",
                    "maxAttempts": 2,
                },
                {
                    "stepId": "dance",
                    "phase": "activity",
                    "skillName": "dance_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "Take part in a dance activity in the plaza",
                    "maxAttempts": 2,
                },
                {
                    "stepId": "balloon",
                    "phase": "transport",
                    "skillName": "hot_air_balloon_auto_schedule",
                    "schemaVersion": "v1",
                    "intent": "Take a hot air balloon ride",
                    "maxAttempts": 2,
                },
                {
                    "stepId": "social-opportunity",
                    "phase": "social",
                    "intent": "Remain available for a nearby friend chat opportunity",
                    "maxAttempts": 1,
                },
            ],
        }
    )


def materialize_activity_plan(
    proposal: ActivityPlanProposal,
    *,
    plan_id: str,
    version: int,
    available_skills: set[tuple[str, str]],
    lobby: bool,
) -> ActivityPlan:
    executable = [step for step in proposal.steps if step.skill_name is not None]
    unavailable = [
        (step.skill_name, step.schema_version)
        for step in executable
        if (step.skill_name, step.schema_version) not in available_skills
    ]
    if unavailable:
        raise ActivityPlanValidationError("activity plan contains skills outside availableSkills")
    if lobby and (
        not executable
        or executable[0].skill_name != "scene_tornado"
        or executable[0].schema_version != "v1"
    ):
        raise ActivityPlanValidationError("Lobby activity plan must start with scene_tornado:v1")
    first = proposal.steps[0]
    try:
        return ActivityPlan.model_validate(
            {
                "planId": plan_id,
                "goalId": proposal.goal_id,
                "goalSummary": proposal.goal_summary,
                "phase": first.phase,
                "status": "active",
                "version": version,
                "currentStepId": first.step_id,
                "steps": [
                    {
                        **step.model_dump(mode="json", by_alias=True),
                        "maxAttempts": 1 if step.skill_name is None else 2,
                        "status": "pending",
                        "attemptCount": 0,
                    }
                    for step in proposal.steps
                ],
            }
        )
    except ValidationError as error:
        raise ActivityPlanValidationError(str(error)) from error


def _replace_step(plan: ActivityPlan, replacement: ActivityPlanStep) -> tuple[ActivityPlanStep, ...]:
    return tuple(replacement if step.step_id == replacement.step_id else step for step in plan.steps)


def _advance_after_terminal(
    plan: ActivityPlan,
    completed_step: ActivityPlanStep,
) -> ActivityPlan:
    current_index = next(
        index for index, step in enumerate(plan.steps) if step.step_id == completed_step.step_id
    )
    steps = _replace_step(plan, completed_step)
    if current_index + 1 >= len(steps):
        return plan.model_copy(
            update={
                "steps": steps,
                "status": "completed",
                "current_step_id": None,
            }
        )
    next_step = steps[current_index + 1]
    return plan.model_copy(
        update={
            "steps": steps,
            "phase": next_step.phase,
            "current_step_id": next_step.step_id,
        }
    )


def record_step_started(plan: ActivityPlan, step_id: str) -> ActivityPlan:
    current = plan.current_step()
    if current.step_id != step_id or current.skill_name is None:
        raise ActivityPlanValidationError("started skill does not match current plan step")
    if current.status not in {"pending", "started"}:
        raise ActivityPlanValidationError("current plan step cannot be started")
    attempts = current.attempt_count if current.status == "started" else current.attempt_count + 1
    replacement = current.model_copy(update={"status": "started", "attempt_count": attempts})
    return plan.model_copy(update={"steps": _replace_step(plan, replacement)})


def record_step_terminal(
    plan: ActivityPlan,
    step_id: str,
    *,
    succeeded: bool,
    retryable: bool = False,
    corrected_decision_allowed: bool = False,
) -> ActivityPlan:
    current = plan.current_step()
    if current.step_id != step_id or current.skill_name is None:
        raise ActivityPlanValidationError("terminal skill does not match current plan step")
    attempts = current.attempt_count if current.status == "started" else current.attempt_count + 1
    if succeeded:
        completed = current.model_copy(update={"status": "succeeded", "attempt_count": attempts})
        return _advance_after_terminal(plan, completed)
    if (retryable or corrected_decision_allowed) and attempts < current.max_attempts:
        pending = current.model_copy(update={"status": "pending", "attempt_count": attempts})
        return plan.model_copy(update={"steps": _replace_step(plan, pending)})
    skipped = current.model_copy(update={"status": "skipped", "attempt_count": attempts})
    return _advance_after_terminal(plan, skipped)


def complete_social_opportunity(plan: ActivityPlan) -> ActivityPlan:
    current = plan.current_step()
    if current.skill_name is not None or current.phase != "social":
        raise ActivityPlanValidationError("current step is not a social opportunity")
    completed = current.model_copy(update={"status": "succeeded"})
    return _advance_after_terminal(plan, completed)
