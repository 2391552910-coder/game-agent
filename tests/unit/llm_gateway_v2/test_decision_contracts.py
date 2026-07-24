import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2CallSkillDecision,
    GatewayV2Decision,
    GatewayV2DecisionAccepted,
    GatewayV2DecisionRejected,
    GatewayV2DecisionResponse,
    GatewayV2NoOpDecision,
    GatewayV2StopHostingDecision,
    GatewayV2WaitDecision,
    parse_gateway_v2_decision,
    parse_gateway_v2_decision_response,
)


def decision_payload(decision_action: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contractVersion": "llm-gateway-http-v2",
        "decisionId": "decision-1",
        "decisionLeaseId": "lease-1",
        "stateVersion": 0,
        "controlGeneration": 1,
        "ttlMs": 30_000,
        "action": decision_action,
    }
    if decision_action == "call_skill":
        payload.update(
            {
                "skillName": "move_to",
                "schemaVersion": "1.0.0",
                "arguments": {
                    "target": {"x": 12.5, "y": 8},
                    "path": ["north", None, True],
                },
            }
        )
    elif decision_action == "wait":
        payload["waitMs"] = 1_000
    payload.update(overrides)
    return payload


VALID_DECISIONS: list[tuple[dict[str, Any], type]] = [
    (decision_payload("call_skill"), GatewayV2CallSkillDecision),
    (decision_payload("wait"), GatewayV2WaitDecision),
    (decision_payload("no_op"), GatewayV2NoOpDecision),
    (decision_payload("stop_hosting"), GatewayV2StopHostingDecision),
]


@pytest.mark.parametrize(("payload", "model_type"), VALID_DECISIONS)
def test_parse_decision_returns_concrete_frozen_wire_model(
    payload: dict[str, Any],
    model_type: type,
) -> None:
    decision = parse_gateway_v2_decision(payload)

    assert type(decision) is model_type
    assert decision.model_dump() == payload
    assert json.loads(decision.model_dump_json()) == payload
    with pytest.raises(ValidationError):
        decision.action = "changed"


@pytest.mark.parametrize(
    "field",
    [
        "contractVersion",
        "decisionId",
        "decisionLeaseId",
        "stateVersion",
        "controlGeneration",
        "ttlMs",
        "action",
    ],
)
@pytest.mark.parametrize("action", ["call_skill", "wait", "no_op", "stop_hosting"])
def test_decision_requires_each_common_field(action: str, field: str) -> None:
    payload = decision_payload(action)
    payload.pop(field)

    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("contractVersion", None),
        ("contractVersion", "llm-gateway-http-v1"),
        ("contractVersion", 2),
        ("decisionId", None),
        ("decisionId", ""),
        ("decisionId", " \t "),
        ("decisionId", 1),
        ("decisionId", "x" * 129),
        ("decisionLeaseId", None),
        ("decisionLeaseId", ""),
        ("decisionLeaseId", " \t "),
        ("decisionLeaseId", 1),
        ("decisionLeaseId", "x" * 129),
        ("stateVersion", None),
        ("stateVersion", -1),
        ("stateVersion", True),
        ("stateVersion", 1.0),
        ("stateVersion", "1"),
        ("controlGeneration", None),
        ("controlGeneration", 0),
        ("controlGeneration", -1),
        ("controlGeneration", True),
        ("controlGeneration", 1.0),
        ("controlGeneration", "1"),
        ("ttlMs", None),
        ("ttlMs", 0),
        ("ttlMs", -1),
        ("ttlMs", True),
        ("ttlMs", 1.0),
        ("ttlMs", "1"),
        ("action", None),
        ("action", "unknown"),
        ("action", 1),
    ],
)
def test_decision_rejects_invalid_common_fields(
    field: str,
    invalid_value: Any,
) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(decision_payload("call_skill", **{field: invalid_value}))


@pytest.mark.parametrize(
    "payload",
    [
        decision_payload("no_op", extra="forbidden"),
        decision_payload("no_op", contract_version="llm-gateway-http-v2"),
        decision_payload("no_op", decision_id="decision-2"),
        decision_payload("no_op", decision_lease_id="lease-2"),
        decision_payload("no_op", state_version=1),
        decision_payload("no_op", control_generation=2),
        decision_payload("no_op", ttl_ms=1_000),
    ],
)
def test_decision_rejects_extra_and_snake_case_fields(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(payload)


@pytest.mark.parametrize("field", ["skillName", "schemaVersion", "arguments"])
def test_call_skill_requires_each_action_field(field: str) -> None:
    payload = decision_payload("call_skill")
    payload.pop(field)

    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("skillName", None),
        ("skillName", ""),
        ("skillName", "  "),
        ("skillName", 1),
        ("skillName", "x" * 129),
        ("schemaVersion", None),
        ("schemaVersion", ""),
        ("schemaVersion", "  "),
        ("schemaVersion", 1),
        ("schemaVersion", "x" * 129),
        ("arguments", None),
        ("arguments", []),
        ("arguments", "{}"),
        ("arguments", 1),
    ],
)
def test_call_skill_rejects_invalid_action_fields(
    field: str,
    invalid_value: Any,
) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(decision_payload("call_skill", **{field: invalid_value}))


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"none": None, "bool": False, "integer": 1, "number": 1.5},
        {"nested": {"list": [1, "two", True, None]}},
    ],
)
def test_call_skill_accepts_json_object_arguments(arguments: dict[str, Any]) -> None:
    decision = parse_gateway_v2_decision(decision_payload("call_skill", arguments=arguments))

    assert decision.model_dump()["arguments"] == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": {1, 2}},
        {"value": b"bytes"},
        {1: "non-string key"},
    ],
)
def test_call_skill_rejects_values_outside_json_domain(arguments: Any) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(decision_payload("call_skill", arguments=arguments))


def test_call_skill_arguments_are_deeply_frozen_and_input_detached() -> None:
    source = {
        "nested": {"items": [1, {"enabled": True}]},
    }
    expected = deepcopy(source)
    decision = parse_gateway_v2_decision(decision_payload("call_skill", arguments=source))

    source["nested"]["items"][1]["enabled"] = False
    source["nested"]["items"].append(2)

    assert decision.model_dump()["arguments"] == expected
    with pytest.raises(TypeError):
        decision.arguments["added"] = True
    with pytest.raises(TypeError):
        decision.arguments["nested"]["items"][1]["enabled"] = False
    with pytest.raises(AttributeError):
        decision.arguments["nested"]["items"].append(2)


@pytest.mark.parametrize("wait_ms", [1, 30_000])
def test_wait_accepts_strict_positive_wait_ms(wait_ms: int) -> None:
    decision = parse_gateway_v2_decision(decision_payload("wait", waitMs=wait_ms))

    assert decision.model_dump()["waitMs"] == wait_ms


@pytest.mark.parametrize("wait_ms", [None, 0, -1, True, 1.0, "1"])
def test_wait_rejects_invalid_wait_ms(wait_ms: Any) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(decision_payload("wait", waitMs=wait_ms))


def test_wait_requires_wait_ms() -> None:
    payload = decision_payload("wait")
    payload.pop("waitMs")

    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(payload)


@pytest.mark.parametrize(
    ("action", "foreign_fields"),
    [
        ("call_skill", {"waitMs": 1}),
        ("wait", {"skillName": "move_to"}),
        ("wait", {"schemaVersion": "1.0.0"}),
        ("wait", {"arguments": {}}),
        ("no_op", {"waitMs": 1}),
        ("no_op", {"skillName": "move_to"}),
        ("no_op", {"schemaVersion": "1.0.0"}),
        ("no_op", {"arguments": {}}),
        ("stop_hosting", {"waitMs": 1}),
        ("stop_hosting", {"skillName": "move_to"}),
        ("stop_hosting", {"schemaVersion": "1.0.0"}),
        ("stop_hosting", {"arguments": {}}),
    ],
)
def test_decision_action_variants_reject_foreign_fields(
    action: str,
    foreign_fields: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision(decision_payload(action, **foreign_fields))


def test_decision_union_json_schema_has_action_discriminator() -> None:
    schema = TypeAdapter(GatewayV2Decision).json_schema()

    assert schema["discriminator"] == {
        "mapping": {
            "call_skill": "#/$defs/GatewayV2CallSkillDecision",
            "no_op": "#/$defs/GatewayV2NoOpDecision",
            "stop_hosting": "#/$defs/GatewayV2StopHostingDecision",
            "wait": "#/$defs/GatewayV2WaitDecision",
        },
        "propertyName": "action",
    }
    assert schema["oneOf"] == [
        {"$ref": "#/$defs/GatewayV2CallSkillDecision"},
        {"$ref": "#/$defs/GatewayV2WaitDecision"},
        {"$ref": "#/$defs/GatewayV2NoOpDecision"},
        {"$ref": "#/$defs/GatewayV2StopHostingDecision"},
    ]


VALID_RESPONSES: list[tuple[dict[str, Any], type]] = [
    (
        {
            "status": "accepted",
            "reason": "decision accepted",
            "skillCallId": "call-1",
        },
        GatewayV2DecisionAccepted,
    ),
    (
        {"status": "accepted", "reason": "decision accepted"},
        GatewayV2DecisionAccepted,
    ),
    (
        {"status": "rejected", "reason": "gateway_policy_changed"},
        GatewayV2DecisionRejected,
    ),
]


@pytest.mark.parametrize(("payload", "model_type"), VALID_RESPONSES)
def test_parse_response_returns_concrete_frozen_wire_model(
    payload: dict[str, Any],
    model_type: type,
) -> None:
    response = parse_gateway_v2_decision_response(payload)

    assert type(response) is model_type
    assert response.model_dump() == payload
    assert json.loads(response.model_dump_json()) == payload
    with pytest.raises(ValidationError):
        response.status = "changed"


@pytest.mark.parametrize(
    "reason",
    [
        "unknown_reason_from_gateway",
        "a newly deployed gateway reason",
        "x" * 1_024,
    ],
)
def test_rejected_accepts_any_nonempty_reason(reason: str) -> None:
    response = parse_gateway_v2_decision_response({"status": "rejected", "reason": reason})

    assert response.reason == reason


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        [],
        "accepted",
        {"reason": "missing status"},
        {"status": None, "reason": "invalid status"},
        {"status": "unknown", "reason": "invalid status"},
        {"status": "accepted"},
        {"status": "rejected"},
        {"status": "accepted", "reason": None},
        {"status": "accepted", "reason": ""},
        {"status": "accepted", "reason": "  "},
        {"status": "accepted", "reason": 1},
        {"status": "accepted", "reason": "x" * 257},
        {"status": "accepted", "reason": "ok", "skillCallId": None},
        {"status": "accepted", "reason": "ok", "skillCallId": ""},
        {"status": "accepted", "reason": "ok", "skillCallId": "  "},
        {"status": "accepted", "reason": "ok", "skillCallId": 1},
        {"status": "accepted", "reason": "ok", "skillCallId": "x" * 129},
        {"status": "rejected", "reason": None},
        {"status": "rejected", "reason": ""},
        {"status": "rejected", "reason": "  "},
        {"status": "rejected", "reason": 1},
        {"status": "rejected", "reason": "no", "skillCallId": "call-1"},
        {"status": "accepted", "reason": "ok", "extra": True},
        {"status": "rejected", "reason": "no", "extra": True},
        {"status": "accepted", "reason": "ok", "skill_call_id": "call-1"},
    ],
)
def test_response_rejects_invalid_field_combinations(payload: Any) -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_decision_response(payload)


def test_accepted_response_skill_call_id_is_omittable_but_not_nullable_in_schema() -> None:
    schema = GatewayV2DecisionAccepted.model_json_schema()
    skill_call_id_schema = schema["properties"]["skillCallId"]

    assert "skillCallId" not in schema["required"]
    assert skill_call_id_schema.get("type") == "string"
    assert "default" not in skill_call_id_schema
    assert "anyOf" not in skill_call_id_schema


def test_response_union_json_schema_has_status_discriminator() -> None:
    schema = TypeAdapter(GatewayV2DecisionResponse).json_schema()

    assert schema["discriminator"] == {
        "mapping": {
            "accepted": "#/$defs/GatewayV2DecisionAccepted",
            "rejected": "#/$defs/GatewayV2DecisionRejected",
        },
        "propertyName": "status",
    }
    assert schema["oneOf"] == [
        {"$ref": "#/$defs/GatewayV2DecisionAccepted"},
        {"$ref": "#/$defs/GatewayV2DecisionRejected"},
    ]


def test_response_model_does_not_apply_request_action_skill_call_id_rules() -> None:
    accepted_without_call = parse_gateway_v2_decision_response({"status": "accepted", "reason": "accepted no-op"})
    accepted_with_call = parse_gateway_v2_decision_response(
        {
            "status": "accepted",
            "reason": "accepted call",
            "skillCallId": "call-1",
        }
    )

    assert accepted_without_call.model_dump() == {
        "status": "accepted",
        "reason": "accepted no-op",
    }
    assert accepted_with_call.model_dump()["skillCallId"] == "call-1"
