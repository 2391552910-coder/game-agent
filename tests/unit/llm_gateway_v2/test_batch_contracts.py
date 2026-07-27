import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2BatchAck,
    GatewayV2BatchEnvelope,
    GatewayV2Error,
    GatewayV2ErrorDetail,
)


def decision_lease_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sessionId": "session-1",
        "controlGeneration": 1,
        "decisionLeaseId": "lease-1",
        "stateVersion": 0,
        "leaseKind": "hosting_control",
        "allowedActions": ["wait"],
        "allowedSkillName": None,
        "allowedSkillNames": [],
        "parentSkillName": None,
    }
    payload.update(overrides)
    return payload


def decision_context_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session": {
            "status": "active",
            "tags": ["initial"],
            "metadata": {"source": "gateway"},
        },
        "availableSkills": [],
        "skillArgumentHints": [],
    }
    payload.update(overrides)
    return payload


def session_started_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "eventId": "event-1",
        "eventType": "session_started",
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": 1,
        "stateVersion": 0,
        "decisionLeaseId": "lease-1",
        "occurredAtMs": 1_700_000_000_000,
        "payload": {
            "reason": "decision_requested",
            "lease": decision_lease_payload(),
            "decisionContext": decision_context_payload(),
        },
    }
    event.update(overrides)
    return event


def skill_finished_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "eventId": "event-2",
        "eventType": "skill_finished",
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": 2,
        "stateVersion": 1,
        "decisionLeaseId": None,
        "occurredAtMs": 1_700_000_000_100,
        "payload": {
            "decisionId": "decision-1",
            "skillName": "move_to",
            "skillCallId": "call-1",
            "status": "success",
            "reason": "ok",
            "failureCategory": None,
            "retryable": False,
            "startedAtMs": 1_700_000_000_099,
            "finishedAtMs": 1_700_000_000_100,
        },
    }
    event.update(overrides)
    return event


def envelope_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "traceId": "trace-1",
        "gatewayId": "gateway-1",
        "contractVersion": "llm-gateway-http-v2",
        "sentAtMs": 1_700_000_000_000,
        "events": [session_started_event()],
    }
    payload.update(overrides)
    return payload


def ack_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "accepted": True,
        "traceId": "trace-1",
        "receivedEventIds": ("event-1", "event-2"),
        "duplicateEventIds": ("event-3",),
    }
    payload.update(overrides)
    return payload


def error_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {"code": "invalid_batch", "message": "Batch is invalid"}
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("field", "wrong_type"),
    [
        ("traceId", 1),
        ("gatewayId", 1),
        ("contractVersion", 1),
        ("sentAtMs", "1"),
        ("events", "not-an-array"),
    ],
)
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_batch_envelope_requires_every_field_with_the_correct_type(
    field: str,
    wrong_type: Any,
    invalid_kind: str,
) -> None:
    payload = envelope_payload()
    if invalid_kind == "missing":
        payload.pop(field)
    elif invalid_kind == "null":
        payload[field] = None
    else:
        payload[field] = wrong_type

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(payload)


def test_batch_envelope_valid_and_default_dump_uses_wire_names() -> None:
    model = GatewayV2BatchEnvelope.model_validate(envelope_payload())

    assert model.model_dump() == envelope_payload()


def test_batch_envelope_json_dump_serializes_events_as_array() -> None:
    model = GatewayV2BatchEnvelope.model_validate(envelope_payload())

    assert model.model_dump(mode="json")["events"] == [session_started_event()]


def test_batch_envelope_copies_event_payload_recursively() -> None:
    event = session_started_event()
    expected = deepcopy(event)
    payload = envelope_payload(events=[event])

    model = GatewayV2BatchEnvelope.model_validate(payload)
    event["eventId"] = "changed"
    event["payload"]["decisionContext"]["session"]["tags"].append("changed")

    assert model.model_dump()["events"] == [expected]


def test_batch_envelope_events_are_deeply_immutable() -> None:
    model = GatewayV2BatchEnvelope.model_validate(
        envelope_payload(
            events=[session_started_event(), skill_finished_event()]
        )
    )

    with pytest.raises(ValidationError):
        model.events[0].event_id = "changed"
    with pytest.raises(TypeError):
        model.events[0].payload.decision_context.session["metadata"]["source"] = "changed"
    with pytest.raises(AttributeError):
        model.events[0].payload.decision_context.session["tags"].append("changed")
    with pytest.raises(ValidationError):
        model.events[1].payload.terminal.status = "changed"


def test_batch_envelope_dump_thaws_nested_containers_to_json_types() -> None:
    model = GatewayV2BatchEnvelope.model_validate(
        envelope_payload(events=[session_started_event(), skill_finished_event()])
    )

    dumped = model.model_dump()

    assert type(dumped) is dict
    assert type(dumped["events"]) is list
    assert type(dumped["events"][0]) is dict
    assert type(dumped["events"][0]["payload"]["decisionContext"]["session"]) is dict
    assert type(dumped["events"][0]["payload"]["decisionContext"]["session"]["tags"]) is list
    assert dumped["events"][1]["payload"]["status"] == "success"


@pytest.mark.parametrize(
    "invalid_value",
    [
        {"not", "json"},
        b"not-json",
        bytearray(b"not-json"),
        {1: "non-string-key"},
        {"nested": {1: "non-string-key"}},
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
    ids=[
        "set",
        "bytes",
        "bytearray",
        "root-non-string-key",
        "nested-non-string-key",
        "nan",
        "positive-infinity",
        "negative-infinity",
    ],
)
def test_batch_envelope_rejects_values_outside_json_domain(
    invalid_value: Any,
) -> None:
    event = session_started_event()
    event["payload"]["decisionContext"]["session"] = {"value": invalid_value}

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(
            envelope_payload(events=[event])
        )


def test_batch_envelope_accepts_and_freezes_complete_json_value_domain() -> None:
    session = {
        "nullValue": None,
        "booleanValue": True,
        "integerValue": 42,
        "floatValue": 1.25,
        "stringValue": "value",
        "listValue": [None, False, 7, 2.5, "item", {"nested": ["value"]}],
        "objectValue": {"key": "value"},
    }
    event = session_started_event()
    event["payload"]["decisionContext"] = decision_context_payload(session=session)

    model = GatewayV2BatchEnvelope.model_validate(envelope_payload(events=[event]))
    dumped = model.model_dump()

    assert dumped["events"][0]["payload"]["decisionContext"]["session"] == session
    assert json.loads(model.model_dump_json())["events"] == [event]
    with pytest.raises(TypeError):
        model.events[0].payload.decision_context.session["objectValue"]["key"] = "changed"
    with pytest.raises(AttributeError):
        model.events[0].payload.decision_context.session["listValue"].append("changed")
    with pytest.raises(AttributeError):
        model.events[0].payload.decision_context.session["listValue"][5]["nested"].append(
            "changed"
        )


@pytest.mark.parametrize(
    "generic_event",
    [
        {},
        {"eventId": "event-1", "kind": "request"},
        {"arbitrary": {"nested": ["json"]}},
    ],
)
def test_batch_envelope_rejects_generic_json_objects(
    generic_event: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(
            envelope_payload(events=[generic_event])
        )


def test_batch_envelope_rejects_unknown_event_type() -> None:
    event = session_started_event(eventType="unknown")

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(events=[event]))


@pytest.mark.parametrize(
    "missing_field",
    [
        "eventId",
        "eventType",
        "sessionId",
        "controlGeneration",
        "eventSequence",
        "stateVersion",
        "decisionLeaseId",
        "occurredAtMs",
        "payload",
    ],
)
def test_batch_envelope_rejects_event_missing_root_field(
    missing_field: str,
) -> None:
    event = session_started_event()
    event.pop(missing_field)

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(events=[event]))


@pytest.mark.parametrize("field", ["traceId", "gatewayId"])
@pytest.mark.parametrize("value", ["", None, 1])
def test_batch_envelope_rejects_invalid_identifiers(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(**{field: value}))


@pytest.mark.parametrize("field", ["traceId", "gatewayId"])
def test_batch_envelope_rejects_identifiers_over_128_chars(field: str) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(**{field: "x" * 129}))


@pytest.mark.parametrize("field", ["traceId", "gatewayId"])
def test_batch_envelope_accepts_identifier_at_128_char_boundary(field: str) -> None:
    model = GatewayV2BatchEnvelope.model_validate(
        envelope_payload(**{field: "x" * 128})
    )

    assert model.model_dump()[field] == "x" * 128


@pytest.mark.parametrize("field", ["traceId", "gatewayId"])
def test_batch_envelope_rejects_whitespace_only_identifiers(field: str) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(**{field: " \t "}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contractVersion", "v2"),
        ("contractVersion", None),
        ("sentAtMs", -1),
        ("sentAtMs", 1.0),
        ("sentAtMs", "1"),
        ("events", ()),
        ("events", None),
        ("events", ("not-an-object",)),
    ],
)
def test_batch_envelope_rejects_invalid_fields(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(**{field: value}))


def test_batch_envelope_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(envelope_payload(extra="forbidden"))


def test_batch_envelope_rejects_snake_case_input() -> None:
    payload = envelope_payload()
    payload["trace_id"] = payload.pop("traceId")

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(payload)


def test_batch_ack_valid_and_default_dump_uses_wire_names_and_arrays() -> None:
    model = GatewayV2BatchAck.model_validate(ack_payload())

    expected = {
        "accepted": True,
        "traceId": "trace-1",
        "receivedEventIds": ["event-1", "event-2"],
        "duplicateEventIds": ["event-3"],
    }
    assert model.model_dump() == expected
    assert model.model_dump(mode="json") == expected


def test_batch_ack_schema_expresses_accepted_as_const_true() -> None:
    accepted_schema = GatewayV2BatchAck.model_json_schema()["properties"]["accepted"]

    assert accepted_schema["const"] is True
    assert accepted_schema.get("enum", [True]) == [True]


@pytest.mark.parametrize(
    ("field", "wrong_type"),
    [
        ("accepted", "true"),
        ("traceId", 1),
        ("receivedEventIds", "event-1"),
        ("duplicateEventIds", "event-2"),
    ],
)
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_batch_ack_requires_every_field_with_the_correct_type(
    field: str,
    wrong_type: Any,
    invalid_kind: str,
) -> None:
    payload = ack_payload()
    if invalid_kind == "missing":
        payload.pop(field)
    elif invalid_kind == "null":
        payload[field] = None
    else:
        payload[field] = wrong_type

    with pytest.raises(ValidationError):
        GatewayV2BatchAck.model_validate(payload)


@pytest.mark.parametrize("accepted", [False, 1, "true", None])
def test_batch_ack_rejects_any_accepted_value_except_strict_true(
    accepted: Any,
) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchAck.model_validate(ack_payload(accepted=accepted))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("traceId", ""),
        ("traceId", None),
        ("receivedEventIds", ("event-1", "event-1")),
        ("duplicateEventIds", ("event-3", "event-3")),
        ("receivedEventIds", ("event-1", 2)),
        ("duplicateEventIds", None),
    ],
)
def test_batch_ack_rejects_invalid_fields(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchAck.model_validate(ack_payload(**{field: value}))


def test_batch_ack_rejects_overlapping_event_ids() -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchAck.model_validate(
            ack_payload(duplicateEventIds=("event-2", "event-3"))
        )


def test_batch_ack_accepts_identifier_length_boundaries() -> None:
    model = GatewayV2BatchAck.model_validate(
        ack_payload(
            traceId="t" * 128,
            receivedEventIds=("r" * 128,),
            duplicateEventIds=("d" * 128,),
        )
    )

    assert model.trace_id == "t" * 128
    assert model.received_event_ids == ("r" * 128,)
    assert model.duplicate_event_ids == ("d" * 128,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("traceId", " \t "),
        ("traceId", "t" * 129),
        ("receivedEventIds", (" \t ",)),
        ("receivedEventIds", ("r" * 129,)),
        ("duplicateEventIds", (" \t ",)),
        ("duplicateEventIds", ("d" * 129,)),
    ],
)
def test_batch_ack_rejects_whitespace_only_or_overlong_identifiers(
    field: str,
    value: Any,
) -> None:
    with pytest.raises(ValidationError):
        GatewayV2BatchAck.model_validate(ack_payload(**{field: value}))


def test_batch_ack_rejects_snake_case_and_extra_fields() -> None:
    payload = ack_payload(extra="forbidden")
    payload["trace_id"] = payload.pop("traceId")

    with pytest.raises(ValidationError):
        GatewayV2BatchAck.model_validate(payload)


def test_batch_ack_is_frozen() -> None:
    model = GatewayV2BatchAck.model_validate(ack_payload())

    with pytest.raises(ValidationError):
        model.trace_id = "trace-2"


def test_gateway_error_valid_and_default_dump_uses_wire_shape() -> None:
    model = GatewayV2Error.model_validate(error_payload())

    assert model.model_dump() == error_payload()
    assert isinstance(model.error, GatewayV2ErrorDetail)


@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_gateway_error_requires_error_object(invalid_kind: str) -> None:
    payload = error_payload()
    if invalid_kind == "missing":
        payload.pop("error")
    elif invalid_kind == "null":
        payload["error"] = None
    else:
        payload["error"] = "not-an-object"

    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate(payload)


@pytest.mark.parametrize("field", ["code", "message"])
@pytest.mark.parametrize("invalid_kind", ["missing", "null", "wrong_type"])
def test_gateway_error_detail_requires_every_field_with_the_correct_type(
    field: str,
    invalid_kind: str,
) -> None:
    detail: dict[str, Any] = {
        "code": "invalid_batch",
        "message": "Batch is invalid",
    }
    if invalid_kind == "missing":
        detail.pop(field)
    elif invalid_kind == "null":
        detail[field] = None
    else:
        detail[field] = 1

    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate({"error": detail})


@pytest.mark.parametrize(
    ("field", "max_length"),
    [("code", 64), ("message", 256)],
)
def test_gateway_error_detail_accepts_string_length_boundaries(
    field: str,
    max_length: int,
) -> None:
    detail = {"code": "invalid_batch", "message": "Batch is invalid"}
    detail[field] = "x" * max_length

    model = GatewayV2Error.model_validate({"error": detail})

    assert getattr(model.error, field) == "x" * max_length


@pytest.mark.parametrize("field", ["code", "message"])
def test_gateway_error_detail_rejects_whitespace_only_strings(field: str) -> None:
    detail = {"code": "invalid_batch", "message": "Batch is invalid"}
    detail[field] = " \t "

    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate({"error": detail})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", ""),
        ("code", "x" * 65),
        ("code", None),
        ("code", 1),
        ("message", ""),
        ("message", "x" * 257),
        ("message", None),
        ("message", 1),
    ],
)
def test_gateway_error_rejects_invalid_detail(field: str, value: Any) -> None:
    detail = {"code": "invalid_batch", "message": "Batch is invalid"}
    detail[field] = value

    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate({"error": detail})


def test_gateway_error_rejects_null_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate({"error": None})

    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate(error_payload(extra="forbidden"))

    with pytest.raises(ValidationError):
        GatewayV2Error.model_validate(
            {"error": {"code": "invalid_batch", "message": "Invalid", "extra": 1}}
        )


def test_gateway_error_detail_rejects_snake_case_input() -> None:
    with pytest.raises(ValidationError):
        GatewayV2ErrorDetail.model_validate(
            {"error_code": "invalid_batch", "message": "Invalid"}
        )


def test_gateway_error_models_are_frozen() -> None:
    model = GatewayV2Error.model_validate(error_payload())

    with pytest.raises(ValidationError):
        model.error.message = "Changed"
