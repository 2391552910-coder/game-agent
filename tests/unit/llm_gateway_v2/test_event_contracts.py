import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from src.core.integration.llm_gateway_v2.contracts import (
    AvailableSkill,
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
        "decisionLeaseId": "lease-1",
        "stateVersion": 0,
        "leaseKind": "hosting_control",
        "allowedDecisionActions": ["call_skill", "wait", "no_op"],
        "session": {
            "status": "active",
            "position": {"x": 1.25, "y": 2},
            "tags": ["initial"],
        },
        "availableSkills": [
            {"skillName": "move", "schemaVersion": "v1"},
            {"skillName": "observe", "schemaVersion": "v2"},
        ],
        "skillArgumentHints": [
            {
                "skillName": "move",
                "schemaVersion": "v1",
                "allowedArgs": ["target.x", "target.y"],
                "missingArgs": ["target.y"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def event_payload(event_type: str, **overrides: Any) -> dict[str, Any]:
    payload_by_type: dict[str, dict[str, Any]] = {
        "session_started": {"lease": lease_payload()},
        "observation_updated": {"lease": lease_payload(stateVersion=1)},
        "skill_started": {
            "decisionId": "decision-1",
            "skillCallId": "call-1",
        },
        "skill_finished": {
            "decisionId": "decision-1",
            "skillCallId": "call-1",
            "terminal": {"status": "success"},
            "lease": lease_payload(stateVersion=2),
        },
        "decision_rejected": {
            "decisionId": "decision-1",
            "reason": "lease expired",
        },
        "session_stopped": {"reason": "hosting stopped"},
    }
    payload: dict[str, Any] = {
        "eventId": f"event-{event_type}",
        "eventType": event_type,
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": 1 if event_type == "session_started" else 2,
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


def test_available_skill_uses_exact_wire_shape() -> None:
    skill = AvailableSkill.model_validate(
        {"skillName": " move ", "schemaVersion": " v1 "}
    )

    assert skill.model_dump() == {"skillName": "move", "schemaVersion": "v1"}
    with pytest.raises(ValidationError):
        skill.skill_name = "observe"


@pytest.mark.parametrize("field", ["skillName", "schemaVersion"])
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_available_skill_requires_strict_fields(
    field: str,
    invalid_kind: str,
) -> None:
    payload: dict[str, Any] = {"skillName": "move", "schemaVersion": "v1"}
    if invalid_kind == "missing":
        payload.pop(field)
    elif invalid_kind == "null":
        payload[field] = None
    else:
        payload[field] = 1

    with pytest.raises(ValidationError):
        AvailableSkill.model_validate(payload)


@pytest.mark.parametrize("field", ["skillName", "schemaVersion"])
@pytest.mark.parametrize("value", ["", " \t ", "x" * 129])
def test_available_skill_rejects_invalid_strings(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        AvailableSkill.model_validate(
            {"skillName": "move", "schemaVersion": "v1", field: value}
        )


def test_available_skill_rejects_extra_and_snake_case_fields() -> None:
    with pytest.raises(ValidationError):
        AvailableSkill.model_validate(
            {"skillName": "move", "schemaVersion": "v1", "extra": 1}
        )
    with pytest.raises(ValidationError):
        AvailableSkill.model_validate(
            {"skill_name": "move", "schemaVersion": "v1"}
        )


def test_skill_argument_hint_uses_exact_wire_shape_and_arrays() -> None:
    hint = SkillArgumentHint.model_validate(
        {
            "skillName": "move",
            "schemaVersion": "v1",
            "allowedArgs": ["target.x", "target.y"],
            "missingArgs": [],
        }
    )

    assert hint.model_dump() == {
        "skillName": "move",
        "schemaVersion": "v1",
        "allowedArgs": ["target.x", "target.y"],
        "missingArgs": [],
    }
    assert hint.allowed_args == ("target.x", "target.y")


@pytest.mark.parametrize(
    ("field", "wrong_type"),
    [
        ("skillName", 1),
        ("schemaVersion", 1),
        ("allowedArgs", "target.x"),
        ("missingArgs", "target.y"),
    ],
)
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_skill_argument_hint_requires_every_field(
    field: str,
    wrong_type: Any,
    invalid_kind: str,
) -> None:
    payload: dict[str, Any] = {
        "skillName": "move",
        "schemaVersion": "v1",
        "allowedArgs": [],
        "missingArgs": [],
    }
    if invalid_kind == "missing":
        payload.pop(field)
    elif invalid_kind == "null":
        payload[field] = None
    else:
        payload[field] = wrong_type

    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(payload)


@pytest.mark.parametrize("field", ["allowedArgs", "missingArgs"])
@pytest.mark.parametrize(
    "value",
    [
        ["path", "path"],
        [""],
        [" \t "],
        ["x" * 129],
        [1],
    ],
)
def test_skill_argument_hint_rejects_invalid_paths(
    field: str,
    value: Any,
) -> None:
    payload: dict[str, Any] = {
        "skillName": "move",
        "schemaVersion": "v1",
        "allowedArgs": [],
        "missingArgs": [],
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(payload)


def test_skill_argument_hint_rejects_extra_and_snake_case_fields() -> None:
    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(
            {
                "skillName": "move",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
                "extra": 1,
            }
        )
    with pytest.raises(ValidationError):
        SkillArgumentHint.model_validate(
            {
                "skill_name": "move",
                "schemaVersion": "v1",
                "allowedArgs": [],
                "missingArgs": [],
            }
        )


def test_decision_lease_context_uses_exact_wire_shape() -> None:
    lease = DecisionLeaseContext.model_validate(lease_payload())

    assert lease.model_dump() == lease_payload()
    assert json.loads(lease.model_dump_json()) == lease_payload()
    assert isinstance(lease.available_skills[0], AvailableSkill)
    assert isinstance(lease.skill_argument_hints[0], SkillArgumentHint)


@pytest.mark.parametrize(
    ("field", "wrong_type"),
    [
        ("decisionLeaseId", 1),
        ("stateVersion", 1.0),
        ("leaseKind", 1),
        ("allowedDecisionActions", "wait"),
        ("session", "not-an-object"),
        ("availableSkills", "not-an-array"),
        ("skillArgumentHints", "not-an-array"),
    ],
)
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_decision_lease_context_requires_every_field(
    field: str,
    wrong_type: Any,
    invalid_kind: str,
) -> None:
    payload = lease_payload()
    if invalid_kind == "missing":
        payload.pop(field)
    elif invalid_kind == "null":
        payload[field] = None
    else:
        payload[field] = wrong_type

    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "snake_name"),
    [
        ("decisionLeaseId", "decision_lease_id"),
        ("stateVersion", "state_version"),
        ("leaseKind", "lease_kind"),
        ("allowedDecisionActions", "allowed_decision_actions"),
        ("availableSkills", "available_skills"),
        ("skillArgumentHints", "skill_argument_hints"),
    ],
)
def test_decision_lease_context_rejects_snake_case_fields(
    field: str,
    snake_name: str,
) -> None:
    payload = lease_payload()
    payload[snake_name] = payload.pop(field)

    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(payload)


def test_decision_lease_context_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(extra="forbidden"))


@pytest.mark.parametrize(
    "actions",
    [
        [],
        ["wait", "wait"],
        ["unsupported"],
        ["wait", 1],
        "wait",
    ],
)
def test_decision_lease_rejects_invalid_allowed_actions(actions: Any) -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(
            lease_payload(allowedDecisionActions=actions)
        )


@pytest.mark.parametrize("value", [-1, True, 1.0, "1"])
def test_decision_lease_state_version_is_strict_nonnegative(value: Any) -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(stateVersion=value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decisionLeaseId", ""),
        ("decisionLeaseId", " \t "),
        ("decisionLeaseId", "x" * 129),
        ("leaseKind", ""),
        ("leaseKind", " \t "),
        ("leaseKind", "x" * 129),
    ],
)
def test_decision_lease_rejects_invalid_strings(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(**{field: value}))


def test_decision_lease_requires_nonempty_session_object() -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(session={}))


@pytest.mark.parametrize(
    "invalid_session",
    [
        {"bad": {"not", "json"}},
        {"bad": bytearray(b"not-json")},
        {1: "non-string-key"},
        {"bad": {1: "non-string-key"}},
        {"bad": float("nan")},
        {"bad": float("inf")},
    ],
)
def test_decision_lease_rejects_session_values_outside_json_domain(
    invalid_session: Any,
) -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(session=invalid_session))


def test_decision_lease_rejects_duplicate_available_skill_names() -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(
            lease_payload(
                availableSkills=[
                    {"skillName": "move", "schemaVersion": "v1"},
                    {"skillName": "move", "schemaVersion": "v2"},
                ],
                skillArgumentHints=[],
            )
        )


def test_decision_lease_rejects_duplicate_hint_skill_names() -> None:
    hint = {
        "skillName": "move",
        "schemaVersion": "v1",
        "allowedArgs": [],
        "missingArgs": [],
    }
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(
            lease_payload(skillArgumentHints=[hint, deepcopy(hint)])
        )


@pytest.mark.parametrize(
    "hint",
    [
        {
            "skillName": "unknown",
            "schemaVersion": "v1",
            "allowedArgs": [],
            "missingArgs": [],
        },
        {
            "skillName": "move",
            "schemaVersion": "v2",
            "allowedArgs": [],
            "missingArgs": [],
        },
    ],
)
def test_decision_lease_hint_must_reference_skill_and_schema_version(
    hint: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        DecisionLeaseContext.model_validate(lease_payload(skillArgumentHints=[hint]))


def test_decision_lease_session_is_deeply_immutable_and_detached() -> None:
    source = lease_payload()
    lease = DecisionLeaseContext.model_validate(source)
    source["session"]["status"] = "changed"
    source["session"]["tags"].append("changed")

    assert lease.model_dump()["session"] == {
        "status": "active",
        "position": {"x": 1.25, "y": 2},
        "tags": ["initial"],
    }
    with pytest.raises(TypeError):
        lease.session["status"] = "changed"
    with pytest.raises(TypeError):
        lease.session["position"]["x"] = 9
    with pytest.raises(AttributeError):
        lease.session["tags"].append("changed")


@pytest.mark.parametrize(
    ("event_type", "required_fields"),
    [
        ("session_started", ["lease"]),
        ("observation_updated", ["lease"]),
        ("skill_started", ["decisionId", "skillCallId"]),
        ("skill_finished", ["decisionId", "skillCallId", "terminal"]),
        ("decision_rejected", ["decisionId", "reason"]),
        ("session_stopped", ["reason"]),
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
        ("skill_started", "skillCallId"),
        ("skill_finished", "decisionId"),
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
    payload["payload"][lease_field] = lease_payload()[lease_field]

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


@pytest.mark.parametrize(
    "terminal",
    [None, {}, "success", [], {"status": float("nan")}, {1: "bad-key"}],
)
def test_skill_finished_rejects_invalid_terminal_union(terminal: Any) -> None:
    payload = event_payload("skill_finished")
    payload["payload"]["terminal"] = terminal

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(payload)


def test_skill_finished_terminal_is_frozen_and_detached() -> None:
    source = event_payload("skill_finished")
    event = parse_gateway_v2_event(source)
    source["payload"]["terminal"]["status"] = "changed"

    assert event.model_dump()["payload"]["terminal"] == {"status": "success"}
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
        envelope.events[0].payload.lease.session["status"] = "changed"
