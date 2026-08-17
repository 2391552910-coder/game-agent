from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer, field_validator, model_validator

from src.core.integration.llm_gateway_v2.contracts import (
    AvailableSkill,
    DecisionAction,
    SkillArgumentHint,
)


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("value must contain only JSON data")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class _GatewayV2AgentModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class _GatewayV2ActionBase(_GatewayV2AgentModel):
    reason: str = Field(default="Model selected this action", min_length=1, max_length=512)
    ttl_ms: int = Field(default=30_000, alias="ttlMs", gt=0)


@dataclass(frozen=True)
class GatewayV2GoalTrackingMetadata:
    user_id: str
    action_type: str
    goal_metric: str
    goal_value: float
    baseline_value: float
    expected_hours: int


class GatewayV2CallSkillAction(_GatewayV2ActionBase):
    action: Literal["call_skill"]
    skill_name: str = Field(alias="skillName", min_length=1, max_length=128)
    schema_version: str = Field(alias="schemaVersion", min_length=1, max_length=128)
    arguments: Mapping[str, Any]
    user_id: str | None = Field(default=None, alias="userId", min_length=1, max_length=255)
    action_type: str | None = Field(default=None, alias="actionType", min_length=1, max_length=100)
    goal_metric: str | None = Field(default=None, alias="goalMetric", min_length=1, max_length=100)
    goal_value: float | None = Field(default=None, alias="goalValue", allow_inf_nan=False)
    baseline_value: float | None = Field(default=None, alias="baselineValue", allow_inf_nan=False)
    expected_hours: int | None = Field(default=None, alias="expectedHours", gt=0)

    @field_validator("arguments")
    @classmethod
    def validate_arguments_json(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = _freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("arguments must be a JSON object")
        return frozen

    @model_validator(mode="after")
    def validate_known_skill_shapes(self) -> GatewayV2CallSkillAction:
        if self.skill_name == "play_action":
            action_id = self.arguments.get("actionId")
            if not isinstance(action_id, str) or not action_id.strip():
                raise ValueError("play_action.arguments.actionId must be a non-empty string")
            if "action" in self.arguments:
                raise ValueError("play_action.arguments.action is not valid in Gateway v2")

        if self.skill_name == "move_to":
            target = self.arguments.get("target")
            if not isinstance(target, Mapping):
                raise ValueError("move_to.arguments.target must be an object")
            for axis in ("x", "y", "z"):
                coordinate = target.get(axis)
                if not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool):
                    raise ValueError(f"move_to.arguments.target.{axis} must be numeric")
        return self

    @field_serializer("arguments")
    def serialize_arguments(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed = _thaw_json(value)
        if not isinstance(thawed, dict):
            raise TypeError("arguments serialization invariant failed")
        return thawed

    def tracking_metadata(self) -> GatewayV2GoalTrackingMetadata | None:
        values = (
            self.user_id,
            self.action_type,
            self.goal_metric,
            self.goal_value,
            self.baseline_value,
            self.expected_hours,
        )
        if any(value is None for value in values):
            return None
        assert self.user_id is not None
        assert self.action_type is not None
        assert self.goal_metric is not None
        assert self.goal_value is not None
        assert self.baseline_value is not None
        assert self.expected_hours is not None
        return GatewayV2GoalTrackingMetadata(
            user_id=self.user_id,
            action_type=self.action_type,
            goal_metric=self.goal_metric,
            goal_value=self.goal_value,
            baseline_value=self.baseline_value,
            expected_hours=self.expected_hours,
        )


class GatewayV2WaitAction(_GatewayV2ActionBase):
    action: Literal["wait"] = "wait"
    wait_ms: int = Field(default=1_000, alias="waitMs", gt=0)


class GatewayV2NoOpAction(_GatewayV2ActionBase):
    action: Literal["no_op"] = "no_op"


class GatewayV2StopHostingAction(_GatewayV2ActionBase):
    action: Literal["stop_hosting"] = "stop_hosting"


GatewayV2AgentAction = Annotated[
    GatewayV2CallSkillAction | GatewayV2WaitAction | GatewayV2NoOpAction | GatewayV2StopHostingAction,
    Field(discriminator="action"),
]
_ACTION_ADAPTER: TypeAdapter[GatewayV2AgentAction] = TypeAdapter(GatewayV2AgentAction)


def parse_gateway_v2_agent_action(value: object) -> GatewayV2AgentAction:
    return _ACTION_ADAPTER.validate_python(value)


class GatewayV2ActionList(_GatewayV2AgentModel):
    actions: tuple[GatewayV2AgentAction, ...] = Field(min_length=1, max_length=5)


class GatewayV2AgentContext(_GatewayV2AgentModel):
    event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    control_generation: int = Field(gt=0)
    event_sequence: int = Field(gt=0)
    decision_lease_id: str = Field(min_length=1, max_length=128)
    state_version: int = Field(ge=0)
    lease_kind: str = Field(min_length=1, max_length=128)
    allowed_decision_actions: tuple[DecisionAction, ...]
    parent_skill_name: str | None = Field(default=None, max_length=128)
    allowed_skill_name: str | None = Field(default=None, max_length=128)
    allowed_skill_names: tuple[str, ...]
    session_snapshot: Mapping[str, Any]
    available_skills: tuple[AvailableSkill, ...]
    skill_argument_hints: tuple[SkillArgumentHint, ...]
    terminal_result: Mapping[str, Any] | None = None

    @field_validator("session_snapshot", "terminal_result")
    @classmethod
    def freeze_context_json(cls, value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if value is None:
            return None
        frozen = _freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("context value must be a JSON object")
        return frozen

    @field_serializer("session_snapshot", "terminal_result")
    def serialize_context_json(self, value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        thawed = _thaw_json(value)
        if not isinstance(thawed, dict):
            raise TypeError("context serialization invariant failed")
        return thawed

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "sessionId": self.session_id,
            "controlGeneration": self.control_generation,
            "eventSequence": self.event_sequence,
            "decisionLeaseId": self.decision_lease_id,
            "stateVersion": self.state_version,
            "leaseKind": self.lease_kind,
            "allowedDecisionActions": list(self.allowed_decision_actions),
            "parentSkillName": self.parent_skill_name,
            "allowedSkillName": self.allowed_skill_name,
            "allowedSkillNames": list(self.allowed_skill_names),
            "session": _thaw_json(self.session_snapshot),
            "availableSkills": [skill.model_dump(mode="json", by_alias=True) for skill in self.available_skills],
            "skillArgumentHints": [hint.model_dump(mode="json", by_alias=True) for hint in self.skill_argument_hints],
            "terminalResult": _thaw_json(self.terminal_result),
        }
