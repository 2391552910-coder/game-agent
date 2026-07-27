from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    Strict,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

StrictPositiveInt = Annotated[int, Strict(), Field(gt=0)]
SUPPORTED_DECISION_ACTIONS: tuple[
    Literal["call_skill"],
    Literal["wait"],
    Literal["no_op"],
    Literal["stop_hosting"],
] = ("call_skill", "wait", "no_op", "stop_hosting")
SUPPORTED_EVENT_TYPES: tuple[
    Literal["session_started"],
    Literal["observation_updated"],
    Literal["skill_started"],
    Literal["skill_finished"],
    Literal["decision_rejected"],
    Literal["session_stopped"],
] = (
    "session_started",
    "observation_updated",
    "skill_started",
    "skill_finished",
    "decision_rejected",
    "session_stopped",
)


class GatewayV2Capabilities(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=False,
        serialize_by_alias=True,
    )

    contract_version: Literal["llm-gateway-http-v2"] = Field(
        default="llm-gateway-http-v2",
        validation_alias="contractVersion",
        serialization_alias="contractVersion",
    )
    receive_events_path: Literal["/api/gateway/v2/events"] = Field(
        default="/api/gateway/v2/events",
        validation_alias="receiveEventsPath",
        serialization_alias="receiveEventsPath",
    )
    supported_decision_actions: tuple[
        Literal["call_skill"],
        Literal["wait"],
        Literal["no_op"],
        Literal["stop_hosting"],
    ] = Field(
        default=SUPPORTED_DECISION_ACTIONS,
        validation_alias="supportedDecisionActions",
        serialization_alias="supportedDecisionActions",
    )
    per_event_ack: StrictBool = Field(
        default=True,
        validation_alias="perEventAck",
        serialization_alias="perEventAck",
    )
    control_generation: StrictBool = Field(
        default=True,
        validation_alias="controlGeneration",
        serialization_alias="controlGeneration",
    )
    event_sequence: StrictBool = Field(
        default=True,
        validation_alias="eventSequence",
        serialization_alias="eventSequence",
    )
    async_skill_terminal: StrictBool = Field(
        default=True,
        validation_alias="asyncSkillTerminal",
        serialization_alias="asyncSkillTerminal",
    )
    supported_event_types: tuple[
        Literal["session_started"],
        Literal["observation_updated"],
        Literal["skill_started"],
        Literal["skill_finished"],
        Literal["decision_rejected"],
        Literal["session_stopped"],
    ] = Field(
        default=SUPPORTED_EVENT_TYPES,
        validation_alias="supportedEventTypes",
        serialization_alias="supportedEventTypes",
    )
    max_event_batch_size: StrictPositiveInt = Field(
        validation_alias="maxEventBatchSize",
        serialization_alias="maxEventBatchSize",
    )
    max_decision_ttl_ms: StrictPositiveInt = Field(
        validation_alias="maxDecisionTtlMs",
        serialization_alias="maxDecisionTtlMs",
    )

    @field_serializer("supported_decision_actions", "supported_event_types")
    def serialize_fixed_collections(self, value: tuple[str, ...]) -> list[str]:
        return list(value)

    @field_validator(
        "per_event_ack",
        "control_generation",
        "event_sequence",
        "async_skill_terminal",
    )
    @classmethod
    def validate_true_capability_flags(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("must be true")
        return value


def build_gateway_v2_capabilities(
    max_event_batch_size: int,
    max_decision_ttl_ms: int,
) -> GatewayV2Capabilities:
    return GatewayV2Capabilities.model_validate(
        {
            "maxEventBatchSize": max_event_batch_size,
            "maxDecisionTtlMs": max_decision_ttl_ms,
        }
    )


StrictNonNegativeInt = Annotated[int, Strict(), Field(ge=0)]
NonEmptyString128 = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        strict=True,
    ),
]
NonEmptyString64 = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        strict=True,
    ),
]
NonEmptyString256 = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        strict=True,
    ),
]
NonEmptyString = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        strict=True,
    ),
]


def _deep_freeze(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("event numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("event object keys must be strings")
            frozen[key] = _deep_freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    raise ValueError("events must contain only JSON values")


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


class _WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=False,
        serialize_by_alias=True,
    )


class SkillExposureDescriptor(_WireModel):
    state: NonEmptyString128
    reason: Annotated[str, Strict()]
    expose_to_admin_default: StrictBool = Field(
        validation_alias="exposeToAdminDefault",
        serialization_alias="exposeToAdminDefault",
    )
    expose_to_decision_provider: StrictBool = Field(
        validation_alias="exposeToDecisionProvider",
        serialization_alias="exposeToDecisionProvider",
    )
    allow_explicit_call: StrictBool = Field(
        validation_alias="allowExplicitCall",
        serialization_alias="allowExplicitCall",
    )


class AvailableSkill(_WireModel):
    skill_name: NonEmptyString128 = Field(
        validation_alias="SkillName",
        serialization_alias="SkillName",
    )
    schema_version: NonEmptyString128 = Field(
        validation_alias="SchemaVersion",
        serialization_alias="SchemaVersion",
    )
    require_running: StrictBool = Field(
        validation_alias="RequireRunning",
        serialization_alias="RequireRunning",
    )
    cooldown_ms: StrictNonNegativeInt = Field(
        validation_alias="CooldownMs",
        serialization_alias="CooldownMs",
    )
    exposure: SkillExposureDescriptor | None = None


class SkillArgumentField(_WireModel):
    path: NonEmptyString128
    argument_type: NonEmptyString128 | None = Field(
        default=None,
        validation_alias="type",
        serialization_alias="type",
    )
    status: NonEmptyString128 | None = None
    source: NonEmptyString128 | None = None
    state_path: NonEmptyString256 | None = Field(
        default=None,
        validation_alias="statePath",
        serialization_alias="statePath",
    )
    reason: NonEmptyString256 | None = None
    next_step: NonEmptyString256 | None = Field(
        default=None,
        validation_alias="nextStep",
        serialization_alias="nextStep",
    )


class SkillArgumentNextStep(_WireModel):
    kind: NonEmptyString128
    label: NonEmptyString256 | None = None
    skill_name: NonEmptyString128 | None = Field(
        default=None,
        validation_alias="skillName",
        serialization_alias="skillName",
    )
    reason: NonEmptyString256 | None = None


class SkillArgumentHint(_WireModel):
    skill_name: NonEmptyString128 = Field(
        validation_alias="skillName",
        serialization_alias="skillName",
    )
    schema_version: NonEmptyString128 = Field(
        validation_alias="schemaVersion",
        serialization_alias="schemaVersion",
    )
    argument_status: NonEmptyString128 = Field(
        validation_alias="argumentStatus",
        serialization_alias="argumentStatus",
    )
    suggested_args: Mapping[str, Any] = Field(
        validation_alias="suggestedArgs",
        serialization_alias="suggestedArgs",
    )
    allowed_args: tuple[SkillArgumentField, ...] = Field(
        validation_alias="allowedArgs",
        serialization_alias="allowedArgs",
    )
    missing_args: tuple[SkillArgumentField, ...] = Field(
        validation_alias="missingArgs",
        serialization_alias="missingArgs",
    )
    warnings: tuple[NonEmptyString256, ...]
    next_steps: tuple[SkillArgumentNextStep, ...] = Field(
        validation_alias="nextSteps",
        serialization_alias="nextSteps",
    )

    @field_validator("allowed_args", "missing_args")
    @classmethod
    def validate_unique_paths(
        cls,
        value: tuple[SkillArgumentField, ...],
    ) -> tuple[SkillArgumentField, ...]:
        paths = [item.path for item in value]
        if len(set(paths)) != len(paths):
            raise ValueError("argument paths must not contain duplicates")
        return value

    @field_validator("suggested_args")
    @classmethod
    def freeze_suggested_args(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], _deep_freeze(value))

    @field_serializer("suggested_args")
    def serialize_suggested_args(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _deep_thaw(value))

    @field_serializer("allowed_args", "missing_args", "next_steps", mode="wrap")
    def serialize_argument_objects(
        self,
        value: tuple[SkillArgumentField, ...] | tuple[SkillArgumentNextStep, ...],
        handler: Any,
    ) -> list[dict[str, Any]]:
        return list(handler(value))

    @field_serializer("warnings")
    def serialize_warnings(self, value: tuple[str, ...]) -> list[str]:
        return list(value)


DecisionAction = Literal["call_skill", "wait", "no_op", "stop_hosting"]


class DecisionLeaseContext(_WireModel):
    session_id: NonEmptyString128 = Field(
        validation_alias="sessionId",
        serialization_alias="sessionId",
    )
    control_generation: StrictPositiveInt = Field(
        validation_alias="controlGeneration",
        serialization_alias="controlGeneration",
    )
    decision_lease_id: NonEmptyString128 = Field(
        validation_alias="decisionLeaseId",
        serialization_alias="decisionLeaseId",
    )
    state_version: StrictNonNegativeInt = Field(
        validation_alias="stateVersion",
        serialization_alias="stateVersion",
    )
    lease_kind: NonEmptyString128 = Field(
        validation_alias="leaseKind",
        serialization_alias="leaseKind",
    )
    allowed_actions: tuple[DecisionAction, ...] = Field(
        min_length=1,
        validation_alias="allowedActions",
        serialization_alias="allowedActions",
    )
    allowed_skill_name: NonEmptyString128 | None = Field(
        validation_alias="allowedSkillName",
        serialization_alias="allowedSkillName",
    )
    allowed_skill_names: tuple[NonEmptyString128, ...] = Field(
        validation_alias="allowedSkillNames",
        serialization_alias="allowedSkillNames",
    )
    parent_skill_name: NonEmptyString128 | None = Field(
        validation_alias="parentSkillName",
        serialization_alias="parentSkillName",
    )

    @field_validator("allowed_actions")
    @classmethod
    def validate_unique_actions(
        cls,
        value: tuple[DecisionAction, ...],
    ) -> tuple[DecisionAction, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowedActions must not contain duplicates")
        return value

    @field_validator("allowed_skill_names")
    @classmethod
    def validate_unique_skill_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowedSkillNames must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_allowed_skill_alias(self) -> "DecisionLeaseContext":
        if (
            self.allowed_skill_name is not None
            and self.allowed_skill_name not in self.allowed_skill_names
        ):
            raise ValueError("allowedSkillName must be included in allowedSkillNames")
        return self

    @field_serializer("allowed_actions", "allowed_skill_names")
    def serialize_allowed_actions(
        self,
        value: tuple[str, ...],
    ) -> list[str]:
        return list(value)


class DecisionContext(_WireModel):
    session: Mapping[str, Any]
    available_skills: tuple[AvailableSkill, ...] = Field(
        validation_alias="availableSkills",
        serialization_alias="availableSkills",
    )
    skill_argument_hints: tuple[SkillArgumentHint, ...] = Field(
        validation_alias="skillArgumentHints",
        serialization_alias="skillArgumentHints",
    )

    @field_validator("session")
    @classmethod
    def freeze_session(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if not value:
            raise ValueError("session must contain at least one field")
        return cast(Mapping[str, Any], _deep_freeze(value))

    @model_validator(mode="after")
    def validate_skill_references(self) -> "DecisionContext":
        skills = {skill.skill_name: skill.schema_version for skill in self.available_skills}
        if len(skills) != len(self.available_skills):
            raise ValueError("availableSkills SkillName values must be unique")

        hinted_names = {hint.skill_name for hint in self.skill_argument_hints}
        if len(hinted_names) != len(self.skill_argument_hints):
            raise ValueError("skillArgumentHints skillName values must be unique")
        for hint in self.skill_argument_hints:
            if skills.get(hint.skill_name) != hint.schema_version:
                raise ValueError("skillArgumentHints must reference an available skill and schema")
        return self

    @field_serializer("session")
    def serialize_session(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _deep_thaw(value))

    @field_serializer("available_skills", "skill_argument_hints", mode="wrap")
    def serialize_skill_collections(
        self,
        value: tuple[AvailableSkill, ...] | tuple[SkillArgumentHint, ...],
        handler: Any,
    ) -> list[dict[str, Any]]:
        return list(handler(value))


class SessionStartedPayload(_WireModel):
    reason: NonEmptyString256
    lease: DecisionLeaseContext
    decision_context: DecisionContext = Field(
        validation_alias="decisionContext",
        serialization_alias="decisionContext",
    )


class ObservationUpdatedPayload(_WireModel):
    reason: NonEmptyString256
    lease: DecisionLeaseContext
    decision_context: DecisionContext = Field(
        validation_alias="decisionContext",
        serialization_alias="decisionContext",
    )


class SkillStartedPayload(_WireModel):
    decision_id: NonEmptyString128 = Field(
        validation_alias="decisionId",
        serialization_alias="decisionId",
    )
    skill_call_id: NonEmptyString128 = Field(
        validation_alias="skillCallId",
        serialization_alias="skillCallId",
    )
    skill_name: NonEmptyString128 = Field(
        validation_alias="skillName",
        serialization_alias="skillName",
    )
    started_at_ms: StrictNonNegativeInt = Field(
        validation_alias="startedAtMs",
        serialization_alias="startedAtMs",
    )


class SkillTerminalSuccess(_WireModel):
    status: Literal["success"]


FailureCategory = Literal[
    "business_rejected",
    "transport_failed",
    "protocol_failed",
    "internal_failed",
]


class SkillTerminalFailed(_WireModel):
    status: Literal["failed"]
    failure_category: FailureCategory = Field(
        validation_alias="failureCategory",
        serialization_alias="failureCategory",
    )
    reason: NonEmptyString256
    retryable: StrictBool


class SkillTerminalCancelled(_WireModel):
    status: Literal["cancelled"]
    reason: NonEmptyString256
    retryable: StrictBool


class SkillTerminalTimeout(_WireModel):
    status: Literal["timeout"]
    reason: NonEmptyString256
    retryable: StrictBool


SkillTerminal = Annotated[
    SkillTerminalSuccess | SkillTerminalFailed | SkillTerminalCancelled | SkillTerminalTimeout,
    Field(discriminator="status"),
]
_SKILL_TERMINAL_ADAPTER: TypeAdapter[SkillTerminal] = TypeAdapter(SkillTerminal)


def parse_skill_terminal(value: object) -> SkillTerminal:
    return _SKILL_TERMINAL_ADAPTER.validate_python(value)


def _omit_internal_default_from_schema(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


class SkillFinishedPayload(_WireModel):
    decision_id: NonEmptyString128 = Field(
        validation_alias="decisionId",
        serialization_alias="decisionId",
    )
    skill_call_id: NonEmptyString128 = Field(
        validation_alias="skillCallId",
        serialization_alias="skillCallId",
    )
    skill_name: NonEmptyString128 = Field(
        validation_alias="skillName",
        serialization_alias="skillName",
    )
    status: Literal["success", "failed", "cancelled", "timeout"]
    reason: NonEmptyString256
    failure_category: FailureCategory | None = Field(
        validation_alias="failureCategory",
        serialization_alias="failureCategory",
    )
    retryable: StrictBool
    started_at_ms: StrictNonNegativeInt = Field(
        validation_alias="startedAtMs",
        serialization_alias="startedAtMs",
    )
    finished_at_ms: StrictNonNegativeInt = Field(
        validation_alias="finishedAtMs",
        serialization_alias="finishedAtMs",
    )
    lease: DecisionLeaseContext | SkipJsonSchema[None] = Field(
        default=None,
        json_schema_extra=_omit_internal_default_from_schema,
    )
    decision_context: DecisionContext | SkipJsonSchema[None] = Field(
        default=None,
        validation_alias="decisionContext",
        serialization_alias="decisionContext",
        json_schema_extra=_omit_internal_default_from_schema,
    )

    @field_validator("lease", "decision_context", mode="before")
    @classmethod
    def reject_explicit_null_decision_data(cls, value: object) -> object:
        if value is None:
            raise ValueError("optional decision data must be omitted instead of null")
        return value

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> "SkillFinishedPayload":
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("finishedAtMs must not be earlier than startedAtMs")
        if self.status == "failed" and self.failure_category is None:
            raise ValueError("failed terminal requires failureCategory")
        if self.status != "failed" and self.failure_category is not None:
            raise ValueError("failureCategory is only valid for failed terminal")
        if (self.lease is None) != (self.decision_context is None):
            raise ValueError("lease and decisionContext must be supplied together")
        return self

    @property
    def terminal(self) -> SkillTerminal:
        payload: dict[str, Any] = {"status": self.status}
        if self.status != "success":
            payload.update({"reason": self.reason, "retryable": self.retryable})
        if self.failure_category is not None:
            payload["failureCategory"] = self.failure_category
        return parse_skill_terminal(payload)

    @model_serializer(mode="wrap")
    def serialize_without_internal_lease_default(
        self,
        handler: Any,
    ) -> dict[str, Any]:
        serialized: dict[str, Any] = handler(self)
        if self.lease is None:
            serialized.pop("lease", None)
            serialized.pop("decisionContext", None)
        return serialized


class DecisionRejectedPayload(_WireModel):
    decision_id: NonEmptyString128 = Field(
        validation_alias="decisionId",
        serialization_alias="decisionId",
    )
    reason: NonEmptyString256


class SessionStoppedPayload(_WireModel):
    reason: NonEmptyString256
    stopped_at_ms: StrictNonNegativeInt = Field(
        validation_alias="stoppedAtMs",
        serialization_alias="stoppedAtMs",
    )


class _GatewayV2EventBase(_WireModel):
    event_id: NonEmptyString128 = Field(
        validation_alias="eventId",
        serialization_alias="eventId",
    )
    session_id: NonEmptyString128 = Field(
        validation_alias="sessionId",
        serialization_alias="sessionId",
    )
    control_generation: StrictPositiveInt = Field(
        validation_alias="controlGeneration",
        serialization_alias="controlGeneration",
    )
    event_sequence: StrictPositiveInt = Field(
        validation_alias="eventSequence",
        serialization_alias="eventSequence",
    )
    state_version: StrictNonNegativeInt = Field(
        validation_alias="stateVersion",
        serialization_alias="stateVersion",
    )
    decision_lease_id: NonEmptyString128 | None = Field(
        validation_alias="decisionLeaseId",
        serialization_alias="decisionLeaseId",
    )
    occurred_at_ms: StrictNonNegativeInt = Field(
        validation_alias="occurredAtMs",
        serialization_alias="occurredAtMs",
    )

    def validate_lease_identity(self, lease: DecisionLeaseContext) -> None:
        if self.session_id != lease.session_id:
            raise ValueError("event sessionId does not match lease")
        if self.control_generation != lease.control_generation:
            raise ValueError("event controlGeneration does not match lease")
        if self.state_version != lease.state_version:
            raise ValueError("event stateVersion does not match lease")
        if self.decision_lease_id != lease.decision_lease_id:
            raise ValueError("event decisionLeaseId does not match lease")


class SessionStartedEvent(_GatewayV2EventBase):
    event_type: Literal["session_started"] = Field(
        validation_alias="eventType",
        serialization_alias="eventType",
    )
    payload: SessionStartedPayload

    @model_validator(mode="after")
    def validate_lease(self) -> "SessionStartedEvent":
        self.validate_lease_identity(self.payload.lease)
        return self

    @field_validator("event_sequence")
    @classmethod
    def validate_first_sequence(cls, value: int) -> int:
        if value != 1:
            raise ValueError("session_started eventSequence must be 1")
        return value


class ObservationUpdatedEvent(_GatewayV2EventBase):
    event_type: Literal["observation_updated"] = Field(
        validation_alias="eventType",
        serialization_alias="eventType",
    )
    payload: ObservationUpdatedPayload

    @model_validator(mode="after")
    def validate_lease(self) -> "ObservationUpdatedEvent":
        self.validate_lease_identity(self.payload.lease)
        return self


class SkillStartedEvent(_GatewayV2EventBase):
    event_type: Literal["skill_started"] = Field(
        validation_alias="eventType",
        serialization_alias="eventType",
    )
    payload: SkillStartedPayload


class SkillFinishedEvent(_GatewayV2EventBase):
    event_type: Literal["skill_finished"] = Field(
        validation_alias="eventType",
        serialization_alias="eventType",
    )
    payload: SkillFinishedPayload

    @model_validator(mode="after")
    def validate_optional_lease(self) -> "SkillFinishedEvent":
        if self.payload.lease is None:
            if self.decision_lease_id is not None:
                raise ValueError("decisionLeaseId requires a skill_finished lease")
        else:
            self.validate_lease_identity(self.payload.lease)
        return self


class DecisionRejectedEvent(_GatewayV2EventBase):
    event_type: Literal["decision_rejected"] = Field(
        validation_alias="eventType",
        serialization_alias="eventType",
    )
    payload: DecisionRejectedPayload


class SessionStoppedEvent(_GatewayV2EventBase):
    event_type: Literal["session_stopped"] = Field(
        validation_alias="eventType",
        serialization_alias="eventType",
    )
    payload: SessionStoppedPayload


GatewayV2Event = Annotated[
    SessionStartedEvent
    | ObservationUpdatedEvent
    | SkillStartedEvent
    | SkillFinishedEvent
    | DecisionRejectedEvent
    | SessionStoppedEvent,
    Field(discriminator="event_type"),
]
_GATEWAY_V2_EVENT_ADAPTER: TypeAdapter[GatewayV2Event] = TypeAdapter(GatewayV2Event)


def parse_gateway_v2_event(value: object) -> GatewayV2Event:
    return _GATEWAY_V2_EVENT_ADAPTER.validate_python(value)


class GatewayV2BatchEnvelope(_WireModel):
    trace_id: NonEmptyString128 = Field(
        validation_alias="traceId",
        serialization_alias="traceId",
    )
    gateway_id: NonEmptyString128 = Field(
        validation_alias="gatewayId",
        serialization_alias="gatewayId",
    )
    contract_version: Literal["llm-gateway-http-v2"] = Field(
        validation_alias="contractVersion",
        serialization_alias="contractVersion",
    )
    sent_at_ms: StrictNonNegativeInt = Field(
        validation_alias="sentAtMs",
        serialization_alias="sentAtMs",
    )
    events: tuple[GatewayV2Event, ...] = Field(min_length=1)

    @field_serializer("events")
    def serialize_events(
        self,
        value: tuple[GatewayV2Event, ...],
        info: SerializationInfo,
    ) -> list[dict[str, Any]]:
        return [
            event.model_dump(
                mode=info.mode,
                by_alias=info.by_alias,
                exclude_unset=info.exclude_unset,
                exclude_defaults=info.exclude_defaults,
                exclude_none=info.exclude_none,
                round_trip=info.round_trip,
                context=info.context,
                serialize_as_any=info.serialize_as_any,
            )
            for event in value
        ]


class GatewayV2BatchAck(_WireModel):
    accepted: Literal[True]
    trace_id: NonEmptyString128 = Field(
        validation_alias="traceId",
        serialization_alias="traceId",
    )
    received_event_ids: tuple[NonEmptyString128, ...] = Field(
        validation_alias="receivedEventIds",
        serialization_alias="receivedEventIds",
    )
    duplicate_event_ids: tuple[NonEmptyString128, ...] = Field(
        validation_alias="duplicateEventIds",
        serialization_alias="duplicateEventIds",
    )

    @field_validator("accepted", mode="before")
    @classmethod
    def validate_accepted(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("must be true")
        return True

    @model_validator(mode="after")
    def validate_event_ids(self) -> "GatewayV2BatchAck":
        received_ids = set(self.received_event_ids)
        duplicate_ids = set(self.duplicate_event_ids)
        if len(received_ids) != len(self.received_event_ids):
            raise ValueError("receivedEventIds must not contain duplicates")
        if len(duplicate_ids) != len(self.duplicate_event_ids):
            raise ValueError("duplicateEventIds must not contain duplicates")
        if received_ids & duplicate_ids:
            raise ValueError("receivedEventIds and duplicateEventIds must not overlap")
        return self

    @field_serializer("received_event_ids", "duplicate_event_ids")
    def serialize_event_ids(self, value: tuple[str, ...]) -> list[str]:
        return list(value)


class GatewayV2ErrorDetail(_WireModel):
    code: NonEmptyString64
    message: NonEmptyString256


class GatewayV2Error(_WireModel):
    error: GatewayV2ErrorDetail


class _GatewayV2DecisionBase(_WireModel):
    contract_version: Literal["llm-gateway-http-v2"] = Field(
        validation_alias="contractVersion",
        serialization_alias="contractVersion",
    )
    decision_id: NonEmptyString128 = Field(
        validation_alias="decisionId",
        serialization_alias="decisionId",
    )
    decision_lease_id: NonEmptyString128 = Field(
        validation_alias="decisionLeaseId",
        serialization_alias="decisionLeaseId",
    )
    state_version: StrictNonNegativeInt = Field(
        validation_alias="stateVersion",
        serialization_alias="stateVersion",
    )
    control_generation: StrictPositiveInt = Field(
        validation_alias="controlGeneration",
        serialization_alias="controlGeneration",
    )
    ttl_ms: StrictPositiveInt = Field(
        validation_alias="ttlMs",
        serialization_alias="ttlMs",
    )


class GatewayV2CallSkillDecision(_GatewayV2DecisionBase):
    action: Literal["call_skill"]
    skill_name: NonEmptyString128 = Field(
        validation_alias="skillName",
        serialization_alias="skillName",
    )
    schema_version: NonEmptyString128 = Field(
        validation_alias="schemaVersion",
        serialization_alias="schemaVersion",
    )
    arguments: Mapping[str, Any]

    @field_validator("arguments")
    @classmethod
    def freeze_arguments(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = _deep_freeze(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("arguments must be a JSON object")
        return frozen

    @field_serializer("arguments")
    def serialize_arguments(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _deep_thaw(value))


class GatewayV2WaitDecision(_GatewayV2DecisionBase):
    action: Literal["wait"]
    wait_ms: StrictPositiveInt = Field(
        validation_alias="waitMs",
        serialization_alias="waitMs",
    )


class GatewayV2NoOpDecision(_GatewayV2DecisionBase):
    action: Literal["no_op"]


class GatewayV2StopHostingDecision(_GatewayV2DecisionBase):
    action: Literal["stop_hosting"]


GatewayV2Decision = Annotated[
    GatewayV2CallSkillDecision | GatewayV2WaitDecision | GatewayV2NoOpDecision | GatewayV2StopHostingDecision,
    Field(discriminator="action"),
]
_GATEWAY_V2_DECISION_ADAPTER: TypeAdapter[GatewayV2Decision] = TypeAdapter(GatewayV2Decision)


def parse_gateway_v2_decision(value: object) -> GatewayV2Decision:
    return _GATEWAY_V2_DECISION_ADAPTER.validate_python(value)


class GatewayV2DecisionAccepted(_WireModel):
    status: Literal["accepted"]
    reason: NonEmptyString256
    skill_call_id: NonEmptyString128 | SkipJsonSchema[None] = Field(
        default=None,
        validation_alias="skillCallId",
        serialization_alias="skillCallId",
        json_schema_extra=_omit_internal_default_from_schema,
    )

    @field_validator("skill_call_id", mode="before")
    @classmethod
    def reject_explicit_null_skill_call_id(cls, value: object) -> object:
        if value is None:
            raise ValueError("skillCallId must be omitted instead of null")
        return value

    @model_serializer(mode="wrap")
    def serialize_without_internal_skill_call_id_default(
        self,
        handler: Any,
    ) -> dict[str, Any]:
        serialized: dict[str, Any] = handler(self)
        if self.skill_call_id is None:
            serialized.pop("skillCallId", None)
        return serialized


class GatewayV2DecisionRejected(_WireModel):
    status: Literal["rejected"]
    reason: NonEmptyString


GatewayV2DecisionResponse = Annotated[
    GatewayV2DecisionAccepted | GatewayV2DecisionRejected,
    Field(discriminator="status"),
]
_GATEWAY_V2_DECISION_RESPONSE_ADAPTER: TypeAdapter[GatewayV2DecisionResponse] = TypeAdapter(GatewayV2DecisionResponse)


def parse_gateway_v2_decision_response(value: object) -> GatewayV2DecisionResponse:
    return _GATEWAY_V2_DECISION_RESPONSE_ADAPTER.validate_python(value)
