from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer, field_validator

from src.core.integration.llm_gateway_v2.canonical import canonical_json_bytes

CANONICAL_NON_CHAT_SKILLS = (
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
SEAT_COFFEE_SEQUENCE = ("seat_sit", "coffee_auto_schedule", "seat_get_out")
_STATE_SCHEMA_VERSION = "llm-gateway-live-plan-state-v1"
_STEP_ID_SEQUENCE = ("sit", "coffee", "get-out")

NonEmptyString = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=256)]


class PlanValidationError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"gateway v2 live plan is invalid: {detail}")


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not (-float("inf") < value < float("inf")):
            raise ValueError("JSON number must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object key must be a string")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("arguments must contain only JSON data")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class _PlanWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False, serialize_by_alias=True)


class _PlanStepWire(_PlanWireModel):
    step_id: NonEmptyString = Field(validation_alias="stepId", serialization_alias="stepId")
    skill_name: NonEmptyString = Field(validation_alias="skillName", serialization_alias="skillName")
    arguments: Mapping[str, Any]

    @field_validator("arguments")
    @classmethod
    def freeze_arguments(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = _freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("arguments must be an object")
        return frozen

    @field_serializer("arguments")
    def serialize_arguments(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _thaw_json(value)


class _ExecutionPlanWire(_PlanWireModel):
    plan_id: NonEmptyString = Field(validation_alias="planId", serialization_alias="planId")
    session_id: NonEmptyString = Field(validation_alias="sessionId", serialization_alias="sessionId")
    gateway_id: NonEmptyString = Field(validation_alias="gatewayId", serialization_alias="gatewayId")
    scene_id: NonEmptyString = Field(validation_alias="sceneId", serialization_alias="sceneId")
    expected_skills: tuple[NonEmptyString, ...] = Field(
        validation_alias="expectedSkills",
        serialization_alias="expectedSkills",
    )
    steps: tuple[_PlanStepWire, ...]

    @field_serializer("expected_skills", "steps", mode="wrap")
    def serialize_tuples(self, value: tuple[Any, ...], handler: Any) -> list[Any]:
        return handler(value)


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    skill_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    session_id: str
    gateway_id: str
    scene_id: str
    expected_skills: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    digest: str


@dataclass(frozen=True)
class PlanState:
    schema_version: str
    plan_id: str
    plan_digest: str
    current_step_id: str | None
    step_outcomes: Mapping[str, Literal["success", "failed"]]


@dataclass(frozen=True)
class PlanExecutionContext:
    session_id: str
    gateway_id: str
    scene_id: str
    decision_lease_id: str
    lease_expires_at_ms: int
    now_ms: int
    seat_state: str


@dataclass(frozen=True)
class PreparedPlanAction:
    step_id: str
    skill_name: str
    schema_version: str
    arguments: Mapping[str, Any]


def validate_execution_plan(value: object) -> ExecutionPlan:
    try:
        wire = _ExecutionPlanWire.model_validate(value)
    except Exception as error:
        raise PlanValidationError("schema") from error
    if wire.expected_skills != CANONICAL_NON_CHAT_SKILLS:
        raise PlanValidationError("expectedSkills must equal the canonical 21-skill catalog")
    if tuple(step.skill_name for step in wire.steps) != SEAT_COFFEE_SEQUENCE:
        raise PlanValidationError("steps must implement the seat-coffee-get-out sequence")
    if tuple(step.step_id for step in wire.steps) != _STEP_ID_SEQUENCE:
        raise PlanValidationError("stepId sequence is invalid")
    if any(step.arguments.get("sceneId") != wire.scene_id for step in wire.steps):
        raise PlanValidationError("step sceneId must match plan sceneId")
    sit_chair_id = wire.steps[0].arguments.get("chairId")
    coffee_name = wire.steps[1].arguments.get("coffeeName")
    get_out_chair_id = wire.steps[2].arguments.get("chairId")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (sit_chair_id, coffee_name, get_out_chair_id)
    ):
        raise PlanValidationError("seat and coffee action arguments are incomplete")
    if sit_chair_id != get_out_chair_id:
        raise PlanValidationError("seat_sit and seat_get_out must target the same chair")
    body = wire.model_dump(mode="json", by_alias=True)
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return ExecutionPlan(
        plan_id=wire.plan_id,
        session_id=wire.session_id,
        gateway_id=wire.gateway_id,
        scene_id=wire.scene_id,
        expected_skills=wire.expected_skills,
        steps=tuple(
            PlanStep(step.step_id, step.skill_name, step.arguments)
            for step in wire.steps
        ),
        digest=digest,
    )


class PlanStateStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load_or_initialize(self, plan: ExecutionPlan) -> PlanState:
        if self._path.exists():
            return self._load(plan)
        state = PlanState(
            schema_version=_STATE_SCHEMA_VERSION,
            plan_id=plan.plan_id,
            plan_digest=plan.digest,
            current_step_id=plan.steps[0].step_id,
            step_outcomes=MappingProxyType({}),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(self._serialize(state))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return self._load(plan)
        return state

    def save(self, plan: ExecutionPlan, state: PlanState) -> None:
        current = self._load(plan)
        if current.plan_id != state.plan_id or current.plan_digest != state.plan_digest:
            raise PlanValidationError("state identity changed")
        self._validate_state(plan, state)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(self._serialize(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self, plan: ExecutionPlan) -> PlanState:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PlanValidationError("state file is corrupt") from error
        if not isinstance(raw, dict) or set(raw) != {
            "schemaVersion",
            "planId",
            "planDigest",
            "currentStepId",
            "stepOutcomes",
        }:
            raise PlanValidationError("state schema")
        outcomes = raw["stepOutcomes"]
        if not isinstance(outcomes, dict) or any(
            not isinstance(key, str) or value not in {"success", "failed"}
            for key, value in outcomes.items()
        ):
            raise PlanValidationError("state outcomes")
        state = PlanState(
            schema_version=raw["schemaVersion"],
            plan_id=raw["planId"],
            plan_digest=raw["planDigest"],
            current_step_id=raw["currentStepId"],
            step_outcomes=MappingProxyType(dict(outcomes)),
        )
        self._validate_state(plan, state)
        return state

    @staticmethod
    def _validate_state(plan: ExecutionPlan, state: PlanState) -> None:
        if state.schema_version != _STATE_SCHEMA_VERSION:
            raise PlanValidationError("state schemaVersion")
        if state.plan_id != plan.plan_id:
            raise PlanValidationError("state planId")
        if state.plan_digest != plan.digest:
            raise PlanValidationError("state plan digest")
        step_ids = {step.step_id for step in plan.steps}
        if state.current_step_id is not None and state.current_step_id not in step_ids:
            raise PlanValidationError("state currentStepId")
        if not set(state.step_outcomes).issubset(step_ids):
            raise PlanValidationError("state stepOutcomes")

    @staticmethod
    def _serialize(state: PlanState) -> str:
        value = {
            "schemaVersion": state.schema_version,
            "planId": state.plan_id,
            "planDigest": state.plan_digest,
            "currentStepId": state.current_step_id,
            "stepOutcomes": dict(state.step_outcomes),
        }
        return canonical_json_bytes(value).decode("utf-8") + "\n"


def prepare_next_action(
    plan: ExecutionPlan,
    state: PlanState,
    context: PlanExecutionContext,
) -> PreparedPlanAction:
    PlanStateStore._validate_state(plan, state)
    if context.session_id != plan.session_id:
        raise PlanValidationError("session mismatch")
    if context.gateway_id != plan.gateway_id:
        raise PlanValidationError("gateway mismatch")
    if context.scene_id != plan.scene_id:
        raise PlanValidationError("scene mismatch")
    if not isinstance(context.decision_lease_id, str) or not context.decision_lease_id.strip():
        raise PlanValidationError("lease is missing")
    if (
        type(context.now_ms) is not int
        or type(context.lease_expires_at_ms) is not int
        or context.now_ms < 0
        or context.lease_expires_at_ms <= context.now_ms
    ):
        raise PlanValidationError("lease is expired")
    if state.current_step_id is None:
        raise PlanValidationError("plan is complete")

    steps_by_id = {step.step_id: step for step in plan.steps}
    step = steps_by_id[state.current_step_id]
    outcomes = state.step_outcomes
    if step.step_id == "sit":
        if outcomes:
            raise PlanValidationError("seat_sit state is inconsistent")
        if context.seat_state.casefold() != "standing":
            raise PlanValidationError("seat_sit requires standing SeatState")
    elif step.step_id == "coffee":
        if outcomes.get("sit") != "success":
            raise PlanValidationError("seat_sit must succeed before coffee")
        if context.seat_state.casefold() != "seated":
            raise PlanValidationError("coffee requires seated SeatState")
    elif step.step_id == "get-out":
        if outcomes.get("sit") != "success" or outcomes.get("coffee") not in {"success", "failed"}:
            raise PlanValidationError("seat_get_out requires terminal coffee result")
        if context.seat_state.casefold() != "seated":
            raise PlanValidationError("seat_get_out requires seated SeatState")

    return PreparedPlanAction(
        step_id=step.step_id,
        skill_name=step.skill_name,
        schema_version="v1",
        arguments=step.arguments,
    )


def record_step_terminal(
    state: PlanState,
    step_id: str,
    *,
    succeeded: bool,
) -> PlanState:
    if state.current_step_id != step_id:
        raise PlanValidationError("terminal step does not match currentStepId")
    if step_id not in _STEP_ID_SEQUENCE:
        raise PlanValidationError("unknown terminal step")
    outcomes = dict(state.step_outcomes)
    outcomes[step_id] = "success" if succeeded else "failed"
    if step_id == "sit":
        next_step_id = "coffee" if succeeded else None
    elif step_id == "coffee":
        next_step_id = "get-out"
    else:
        next_step_id = None
    return replace(
        state,
        current_step_id=next_step_id,
        step_outcomes=MappingProxyType(outcomes),
    )


def record_step_success(state: PlanState, step_id: str) -> PlanState:
    return record_step_terminal(state, step_id, succeeded=True)
