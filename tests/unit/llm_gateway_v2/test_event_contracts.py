import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from src.core.integration.llm_gateway_v2.contracts import (
    AvailableSkill,
    DecisionContext,
    DecisionLeaseContext,
    DecisionRejectedEvent,
    GatewayV2BatchEnvelope,
    ObservationUpdatedEvent,
    SessionStartedEvent,
    SessionStoppedEvent,
    SkillArgumentHint,
    SkillFinishedEvent,
    SkillFinishedPayload,
    SkillStartedEvent,
    parse_gateway_v2_event,
)


def lease_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sessionId": "session-1",
        "controlGeneration": 1,
        "decisionLeaseId": "lease-1",
        "stateVersion": 0,
        "leaseKind": "hosting_control",
        "allowedActions": ["call_skill", "wait", "no_op"],
        "allowedSkillName": "move",
        "allowedSkillNames": ["move", "observe"],
        "parentSkillName": None,
    }
    payload.update(overrides)
    return payload


def skill_descriptor(name: str, schema_version: str) -> dict[str, Any]:
    return {
        "SkillName": name,
        "SchemaVersion": schema_version,
        "RequireRunning": True,
        "CooldownMs": 0,
        "exposure": {
            "state": "Enabled",
            "reason": "",
            "exposeToAdminDefault": True,
            "exposeToDecisionProvider": True,
            "allowExplicitCall": True,
        },
    }


def argument_hint(
    name: str = "move",
    schema_version: str = "v1",
) -> dict[str, Any]:
    def field(path: str, status: str) -> dict[str, Any]:
        return {
            "path": path,
            "type": "number",
            "status": status,
            "source": "contract",
            "statePath": None,
            "reason": None,
            "nextStep": None,
        }

    return {
        "skillName": name,
        "schemaVersion": schema_version,
        "argumentStatus": "ready",
        "suggestedArgs": {},
        "allowedArgs": [field("target.x", "allowed"), field("target.y", "allowed")],
        "missingArgs": [field("target.y", "missing")],
        "warnings": [],
        "nextSteps": [],
    }


def decision_context_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session": {
            "AccountId": "account-1",
            "SessionId": "session-1",
            "SceneId": "scene-1",
            "State": "active",
            "SeatState": "standing",
            "Position": {"x": 1.25, "y": 2},
        },
        "availableSkills": [
            skill_descriptor("move", "v1"),
            skill_descriptor("observe", "v2"),
        ],
        "skillArgumentHints": [argument_hint()],
        "lastSkillResult": None,
    }
    payload.update(overrides)
    return payload


def event_payload(event_type: str, **overrides: Any) -> dict[str, Any]:
    lease_state_version = 0 if event_type == "session_started" else 1
    decision_payload = {
        "reason": "decision_requested",
        "lease": lease_payload(stateVersion=lease_state_version),
        "decisionContext": decision_context_payload(),
    }
    payload_by_type: dict[str, dict[str, Any]] = {
        "session_started": deepcopy(decision_payload),
        "observation_updated": deepcopy(decision_payload),
        "skill_started": {
            "decisionId": "decision-1",
            "skillName": "move",
            "skillCallId": "call-1",
            "startedAtMs": 1_700_000_000_000,
        },
        "skill_finished": {
            "decisionId": "decision-1",
            "skillName": "move",
            "skillCallId": "call-1",
            "status": "success",
            "reason": "ok",
            "failureCategory": None,
            "retryable": False,
            "startedAtMs": 1_700_000_000_000,
            "finishedAtMs": 1_700_000_000_001,
            "lease": lease_payload(stateVersion=lease_state_version),
            "decisionContext": decision_context_payload(),
        },
        "decision_rejected": {
            "decisionId": "decision-1",
            "action": "call_skill",
            "skillName": None,
            "reason": "lease expired",
            "rejectedAtMs": 1_700_000_000_000,
        },
        "session_stopped": {
            "reason": "hosting stopped",
            "stoppedAtMs": 1_700_000_000_000,
        },
    }
    payload: dict[str, Any] = {
        "eventId": f"event-{event_type}",
        "eventType": event_type,
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": 1 if event_type == "session_started" else 2,
        "stateVersion": lease_state_version,
        "decisionLeaseId": (
            "lease-1"
            if event_type in {"session_started", "observation_updated", "skill_finished"}
            else None
        ),
        "occurredAtMs": 1_700_000_000_000,
        "payload": payload_by_type[event_type],
    }
    payload.update(overrides)
    return payload


EVENT_CASES = [
    ("session_started", SessionStartedEvent),
    ("observation_updated", ObservationUpdatedEvent),
    ("skill_started", SkillStartedEvent),
    ("skill_finished", SkillFinishedEvent),
    ("decision_rejected", DecisionRejectedEvent),
    ("session_stopped", SessionStoppedEvent),
]


@pytest.mark.parametrize(("event_type", "model_type"), EVENT_CASES)
def test_parse_accepts_each_event_type_and_uses_concrete_model(
    event_type: str,
    model_type: type,
) -> None:
    payload = event_payload(event_type)

    event = parse_gateway_v2_event(payload)

    assert type(event) is model_type
    assert event.model_dump() == payload
    assert json.loads(event.model_dump_json()) == payload


@pytest.mark.parametrize(("event_type", "model_type"), EVENT_CASES)
@pytest.mark.parametrize(
    ("field", "wrong_type"),
    [
        ("eventId", 1),
        ("eventType", 1),
        ("sessionId", 1),
        ("controlGeneration", 1.0),
        ("eventSequence", 1.0),
        ("stateVersion", 1.0),
        ("occurredAtMs", 1.0),
        ("payload", "not-an-object"),
    ],
)
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_each_event_requires_every_root_field_with_the_correct_type(
    event_type: str,
    model_type: type,
    field: str,
    wrong_type: Any,
    invalid_kind: str,
) -> None:
    payload = event_payload(event_type)
    if invalid_kind == "missing":
        payload.pop(field)
    elif invalid_kind == "null":
        payload[field] = None
    else:
        payload[field] = wrong_type

    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


@pytest.mark.parametrize(("event_type", "model_type"), EVENT_CASES)
def test_each_event_rejects_extra_root_fields(
    event_type: str,
    model_type: type,
) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate(event_payload(event_type, extra="forbidden"))


@pytest.mark.parametrize(("event_type", "model_type"), EVENT_CASES)
@pytest.mark.parametrize(
    ("wire_name", "snake_name"),
    [
        ("eventId", "event_id"),
        ("eventType", "event_type"),
        ("sessionId", "session_id"),
        ("controlGeneration", "control_generation"),
        ("eventSequence", "event_sequence"),
        ("stateVersion", "state_version"),
        ("decisionLeaseId", "decision_lease_id"),
        ("occurredAtMs", "occurred_at_ms"),
    ],
)
def test_each_event_rejects_snake_case_root_fields(
    event_type: str,
    model_type: type,
    wire_name: str,
    snake_name: str,
) -> None:
    payload = event_payload(event_type)
    payload[snake_name] = payload.pop(wire_name)

    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


@pytest.mark.parametrize(("event_type", "model_type"), EVENT_CASES)
@pytest.mark.parametrize("payload_value", [None, {}, {"extra": "forbidden"}])
def test_each_event_rejects_null_empty_or_extra_only_payload(
    event_type: str,
    model_type: type,
    payload_value: Any,
) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate(event_payload(event_type, payload=payload_value))


@pytest.mark.parametrize(("event_type", "model_type"), EVENT_CASES)
def test_concrete_event_rejects_wrong_discriminator(
    event_type: str,
    model_type: type,
) -> None:
    wrong_type = "session_stopped" if event_type != "session_stopped" else "skill_started"

    with pytest.raises(ValidationError):
        model_type.model_validate(event_payload(event_type, eventType=wrong_type))


@pytest.mark.parametrize("event_type", ["unknown", "", None, 1])
def test_parse_rejects_invalid_discriminator(event_type: Any) -> None:
    payload = event_payload("skill_started", eventType=event_type)

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize("field", ["eventId", "sessionId"])
@pytest.mark.parametrize("value", ["", " \t ", "x" * 129])
def test_event_identifiers_reject_empty_whitespace_and_overlong_values(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_event(event_payload("skill_started", **{field: value}))


@pytest.mark.parametrize("field", ["controlGeneration", "eventSequence"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1"])
def test_event_positive_integers_are_strict(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_event(event_payload("skill_started", **{field: value}))


@pytest.mark.parametrize("value", [-1, True, 1.0, "1"])
def test_event_occurred_at_ms_is_strict_nonnegative(value: Any) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_event(event_payload("skill_started", occurredAtMs=value))


def test_event_integer_boundaries_are_accepted() -> None:
    event = parse_gateway_v2_event(
        event_payload(
            "skill_started",
            controlGeneration=1,
            eventSequence=1,
            occurredAtMs=0,
        )
    )

    assert event.control_generation == 1
    assert event.event_sequence == 1
    assert event.occurred_at_ms == 0


@pytest.mark.parametrize("event_sequence", [0, 2, 100])
def test_session_started_requires_first_event_sequence(event_sequence: int) -> None:
    with pytest.raises(ValidationError):
        SessionStartedEvent.model_validate(
            event_payload("session_started", eventSequence=event_sequence)
        )


def test_available_skill_uses_complete_pascal_case_wire_shape() -> None:
    payload = skill_descriptor("move", "v1")
    skill = AvailableSkill.model_validate(payload)

    assert skill.model_dump() == payload
    with pytest.raises(ValidationError):
        AvailableSkill.model_validate(
            {"skillName": "move", "schemaVersion": "v1"}
        )


@pytest.mark.parametrize(
    "field",
    ["SkillName", "SchemaVersion", "RequireRunning", "CooldownMs"],
)
def test_available_skill_requires_complete_descriptor(field: str) -> None:
    payload = skill_descriptor("move", "v1")
    payload.pop(field)

    with pytest.raises(ValidationError):
        AvailableSkill.model_validate(payload)


def test_skill_argument_hint_uses_object_argument_fields() -> None:
    payload = argument_hint()
    hint = SkillArgumentHint.model_validate(payload)

    assert hint.model_dump() == payload
    assert tuple(field.path for field in hint.allowed_args) == (
        "target.x",
        "target.y",
    )
    invalid = argument_hint()
    invalid["allowedArgs"] = ["target.x"]
    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(invalid)


def test_skill_argument_hint_preserves_explicit_integer_range() -> None:
    payload = argument_hint("dance_auto_schedule", "v1")
    payload["allowedArgs"] = [
        {
            "path": "score",
            "type": "integer",
            "status": "allowed",
            "source": "contract",
            "statePath": None,
            "reason": None,
            "nextStep": None,
            "minimum": 1,
            "maximum": 50,
        }
    ]
    payload["missingArgs"] = [{"path": "score"}]

    hint = SkillArgumentHint.model_validate(payload)

    assert hint.allowed_args[0].minimum == 1
    assert hint.allowed_args[0].maximum == 50
    serialized = hint.model_dump(mode="json", by_alias=True)
    assert serialized["allowedArgs"] == payload["allowedArgs"]
    assert "minimum" not in serialized["missingArgs"][0]
    assert "maximum" not in serialized["missingArgs"][0]


@pytest.mark.parametrize(
    "range_fields",
    [
        {"minimum": 1},
        {"maximum": 50},
        {"minimum": 51, "maximum": 50},
        {"minimum": True, "maximum": 50},
        {"minimum": 1, "maximum": 50.0},
    ],
)
def test_skill_argument_hint_rejects_incomplete_or_invalid_integer_range(
    range_fields: dict[str, object],
) -> None:
    payload = argument_hint("dance_auto_schedule", "v1")
    payload["allowedArgs"] = [{"path": "score", **range_fields}]
    payload["missingArgs"] = [{"path": "score"}]

    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(payload)


@pytest.mark.parametrize("field", ["allowedArgs", "missingArgs"])
def test_skill_argument_hint_rejects_duplicate_paths(field: str) -> None:
    payload = argument_hint()
    payload[field] = [{"path": "target.x"}, {"path": "target.x"}]

    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(payload)


def test_decision_lease_context_uses_only_authorization_fields() -> None:
    payload = lease_payload()
    lease = DecisionLeaseContext.model_validate(payload)

    assert lease.model_dump() == payload
    assert lease.allowed_actions == ("call_skill", "wait", "no_op")
    assert lease.allowed_skill_names == ("move", "observe")
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(
            lease_payload(session=decision_context_payload()["session"])
        )


@pytest.mark.parametrize(
    "field",
    [
        "sessionId",
        "controlGeneration",
        "decisionLeaseId",
        "stateVersion",
        "leaseKind",
        "allowedActions",
        "allowedSkillName",
        "allowedSkillNames",
        "parentSkillName",
    ],
)
def test_decision_lease_context_requires_formal_fields(field: str) -> None:
    payload = lease_payload()
    payload.pop(field)

    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(payload)


@pytest.mark.parametrize(
    "actions",
    [[], ["wait", "wait"], ["unsupported"], ["wait", 1], "wait"],
)
def test_decision_lease_rejects_invalid_allowed_actions(actions: Any) -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(allowedActions=actions))


def test_decision_lease_rejects_skill_alias_outside_allowlist() -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(
            lease_payload(allowedSkillName="jump")
        )


def test_decision_context_owns_and_freezes_session_and_skill_metadata() -> None:
    source = decision_context_payload()
    context = DecisionContext.model_validate(source)
    source["session"]["State"] = "changed"
    source["session"]["Position"]["x"] = 99

    assert context.model_dump() == decision_context_payload()
    assert isinstance(context.available_skills[0], AvailableSkill)
    assert isinstance(context.skill_argument_hints[0], SkillArgumentHint)
    with pytest.raises(TypeError):
        context.session["State"] = "changed"
    with pytest.raises(TypeError):
        context.session["Position"]["x"] = 99


def test_decision_context_requires_nullable_last_skill_result() -> None:
    missing = decision_context_payload()
    missing.pop("lastSkillResult")
    with pytest.raises(ValidationError):
        DecisionContext.model_validate(missing)

    null_context = DecisionContext.model_validate(decision_context_payload(lastSkillResult=None))
    result_context = DecisionContext.model_validate(
        decision_context_payload(
            lastSkillResult={
                "decisionId": "decision-previous",
                "skillCallId": "call-previous",
                "skillName": "jump",
                "status": "success",
            }
        )
    )

    assert null_context.last_skill_result is None
    assert result_context.model_dump()["lastSkillResult"]["skillName"] == "jump"


def test_observation_reason_is_nullable_but_other_reasons_are_not() -> None:
    observation = event_payload("observation_updated")
    observation["payload"]["reason"] = None

    parsed = parse_gateway_v2_event(observation)

    assert parsed.payload.reason is None
    for event_type in ("session_started", "skill_finished", "decision_rejected", "session_stopped"):
        payload = event_payload(event_type)
        payload["payload"]["reason"] = None
        with pytest.raises(ValidationError):
            parse_gateway_v2_event(payload)


def test_decision_rejected_accepts_full_payload_with_nullable_skill_name() -> None:
    event = parse_gateway_v2_event(event_payload("decision_rejected"))

    assert event.payload.model_dump() == {
        "decisionId": "decision-1",
        "action": "call_skill",
        "skillName": None,
        "reason": "lease expired",
        "rejectedAtMs": 1_700_000_000_000,
    }


def test_decision_context_rejects_hint_not_published_by_gateway() -> None:
    with pytest.raises(ValidationError):
        DecisionContext.model_validate(
            decision_context_payload(
                skillArgumentHints=[argument_hint("unknown", "v1")]
            )
        )


@pytest.mark.parametrize(
    ("event_type", "required_fields"),
    [
        ("session_started", ["reason", "lease", "decisionContext"]),
        ("observation_updated", ["reason", "lease", "decisionContext"]),
        ("skill_started", ["decisionId", "skillName", "skillCallId", "startedAtMs"]),
        (
            "skill_finished",
            [
                "decisionId",
                "skillName",
                "skillCallId",
                "status",
                "reason",
                "failureCategory",
                "retryable",
                "startedAtMs",
                "finishedAtMs",
            ],
        ),
        ("decision_rejected", ["decisionId", "action", "skillName", "reason", "rejectedAtMs"]),
        ("session_stopped", ["reason", "stoppedAtMs"]),
    ],
)
def test_each_payload_requires_its_type_specific_fields(
    event_type: str,
    required_fields: list[str],
) -> None:
    for field in required_fields:
        payload = event_payload(event_type)
        payload["payload"].pop(field)

        with pytest.raises(ValidationError):
            parse_gateway_v2_event(payload)


@pytest.mark.parametrize(
    ("event_type", "field"),
    [
        ("skill_started", "decisionId"),
        ("skill_started", "skillName"),
        ("skill_started", "skillCallId"),
        ("skill_finished", "decisionId"),
        ("skill_finished", "skillName"),
        ("skill_finished", "skillCallId"),
        ("decision_rejected", "decisionId"),
        ("decision_rejected", "reason"),
        ("session_stopped", "reason"),
    ],
)
@pytest.mark.parametrize("value", [None, 1, "", " \t "])
def test_type_specific_strings_are_strict_nonempty_and_bounded(
    event_type: str,
    field: str,
    value: Any,
) -> None:
    payload = event_payload(event_type)
    payload["payload"][field] = value

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize(
    ("event_type", "foreign_field"),
    [
        ("session_started", "skillCallId"),
        ("observation_updated", "skillCallId"),
        ("skill_started", "lease"),
        ("decision_rejected", "skillCallId"),
        ("session_stopped", "skillCallId"),
    ],
)
def test_event_payload_rejects_fields_owned_by_other_event_types(
    event_type: str,
    foreign_field: str,
) -> None:
    payload = event_payload(event_type)
    payload["payload"][foreign_field] = "unexpected"

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize(
    ("event_type", "field", "overlong"),
    [
        ("skill_started", "decisionId", "x" * 129),
        ("skill_started", "skillCallId", "x" * 129),
        ("skill_finished", "decisionId", "x" * 129),
        ("skill_finished", "skillCallId", "x" * 129),
        ("decision_rejected", "decisionId", "x" * 129),
        ("decision_rejected", "reason", "x" * 257),
        ("session_stopped", "reason", "x" * 257),
    ],
)
def test_type_specific_strings_reject_values_over_their_limits(
    event_type: str,
    field: str,
    overlong: str,
) -> None:
    payload = event_payload(event_type)
    payload["payload"][field] = overlong

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize(
    "event_type",
    ["skill_started", "decision_rejected", "session_stopped"],
)
def test_non_lease_events_reject_lease(event_type: str) -> None:
    payload = event_payload(event_type)
    payload["payload"]["lease"] = lease_payload()

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


def test_skill_finished_accepts_missing_or_complete_lease() -> None:
    without_lease = event_payload("skill_finished")
    without_lease["payload"].pop("lease")
    without_lease["payload"].pop("decisionContext")
    without_lease["decisionLeaseId"] = None

    no_lease_event = parse_gateway_v2_event(without_lease)
    lease_event = parse_gateway_v2_event(event_payload("skill_finished"))

    assert no_lease_event.payload.lease is None
    assert no_lease_event.model_dump() == without_lease
    assert lease_event.payload.lease is not None


def test_skill_finished_lease_schema_is_optional_but_not_nullable() -> None:
    schema = SkillFinishedPayload.model_json_schema()
    lease_schema = schema["properties"]["lease"]

    assert "lease" not in schema["required"]
    assert lease_schema["$ref"] == "#/$defs/DecisionLeaseContext"
    assert "default" not in lease_schema
    assert "anyOf" not in lease_schema


def test_skill_finished_missing_lease_is_omitted_from_all_wire_dumps() -> None:
    payload = event_payload("skill_finished")
    payload["payload"].pop("lease")
    payload["payload"].pop("decisionContext")

    model = SkillFinishedPayload.model_validate(payload["payload"])

    assert model.model_dump() == payload["payload"]
    assert json.loads(model.model_dump_json()) == payload["payload"]


def test_skill_finished_rejects_explicit_null_lease() -> None:
    payload = event_payload("skill_finished")
    payload["payload"]["lease"] = None

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize("lease_field", list(lease_payload()))
def test_skill_finished_rejects_partial_lease_object(lease_field: str) -> None:
    payload = event_payload("skill_finished")
    payload["payload"]["lease"].pop(lease_field)

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize("lease_field", list(lease_payload()))
def test_skill_finished_rejects_flattened_lease_fields(lease_field: str) -> None:
    payload = event_payload("skill_finished")
    payload["payload"].pop("lease")
    payload["payload"].pop("decisionContext")
    payload["decisionLeaseId"] = None
    payload["payload"][lease_field] = lease_payload()[lease_field]

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize("status", [None, "rejected", "unknown", 1])
def test_skill_finished_rejects_invalid_flat_terminal_status(status: Any) -> None:
    payload = event_payload("skill_finished")
    payload["payload"]["status"] = status

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


def test_skill_finished_terminal_view_is_frozen_and_not_serialized() -> None:
    source = event_payload("skill_finished")
    event = parse_gateway_v2_event(source)

    assert "terminal" not in event.model_dump()["payload"]
    assert event.payload.terminal.status == "success"
    with pytest.raises(ValidationError):
        event.payload.terminal.status = "changed"


def test_batch_envelope_parses_mixed_events_and_serializes_wire_json() -> None:
    events = [event_payload(event_type) for event_type, _ in EVENT_CASES]
    envelope_payload = {
        "traceId": "trace-1",
        "gatewayId": "gateway-1",
        "contractVersion": "llm-gateway-http-v2",
        "sentAtMs": 1_700_000_000_100,
        "events": events,
    }

    envelope = GatewayV2BatchEnvelope.model_validate(envelope_payload)

    assert [type(event) for event in envelope.events] == [
        model_type for _, model_type in EVENT_CASES
    ]
    assert envelope.model_dump() == envelope_payload
    assert json.loads(envelope.model_dump_json()) == envelope_payload


def test_batch_envelope_events_are_frozen_models() -> None:
    envelope = GatewayV2BatchEnvelope.model_validate(
        {
            "traceId": "trace-1",
            "gatewayId": "gateway-1",
            "contractVersion": "llm-gateway-http-v2",
            "sentAtMs": 1_700_000_000_100,
            "events": [event_payload("session_started")],
        }
    )

    with pytest.raises(ValidationError):
        envelope.events[0].event_id = "changed"
    with pytest.raises(TypeError):
        envelope.events[0].payload.decision_context.session["status"] = "changed"
