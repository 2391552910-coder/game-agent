import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2Capabilities,
    build_gateway_v2_capabilities,
)

EXPECTED_CAPABILITIES = {
    "contractVersion": "llm-gateway-http-v2",
    "receiveEventsPath": "/api/gateway/v2/events",
    "supportedDecisionActions": ["call_skill", "wait", "no_op", "stop_hosting"],
    "perEventAck": True,
    "controlGeneration": True,
    "eventSequence": True,
    "asyncSkillTerminal": True,
    "supportedEventTypes": [
        "session_started",
        "observation_updated",
        "skill_started",
        "skill_finished",
        "decision_rejected",
        "session_stopped",
        "chat_received",
        "nearby_friend_chat_requested",
        "chat_send_result",
    ],
    "maxEventBatchSize": 64,
    "maxDecisionTtlMs": 30_000,
}


def test_factory_builds_exact_capabilities_contract() -> None:
    capabilities = build_gateway_v2_capabilities(
        max_event_batch_size=64,
        max_decision_ttl_ms=30_000,
    )

    assert isinstance(capabilities, GatewayV2Capabilities)
    assert capabilities.model_dump(by_alias=True) == EXPECTED_CAPABILITIES
    assert list(capabilities.model_dump(by_alias=True)) == list(EXPECTED_CAPABILITIES)


def test_default_serialization_uses_camel_case_wire_contract() -> None:
    capabilities = build_gateway_v2_capabilities(
        max_event_batch_size=64,
        max_decision_ttl_ms=30_000,
    )

    assert capabilities.model_dump() == EXPECTED_CAPABILITIES
    assert json.loads(capabilities.model_dump_json()) == EXPECTED_CAPABILITIES


@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("max_event_batch_size", 65),
        ("max_decision_ttl_ms", 30_001),
        ("per_event_ack", False),
    ],
)
def test_capabilities_fields_cannot_be_assigned_after_construction(
    field: str,
    new_value: object,
) -> None:
    capabilities = build_gateway_v2_capabilities(
        max_event_batch_size=64,
        max_decision_ttl_ms=30_000,
    )

    with pytest.raises(ValidationError):
        setattr(capabilities, field, new_value)


def test_fixed_capability_collections_are_deeply_immutable() -> None:
    capabilities = build_gateway_v2_capabilities(
        max_event_batch_size=64,
        max_decision_ttl_ms=30_000,
    )

    assert capabilities.supported_decision_actions == (
        "call_skill",
        "wait",
        "no_op",
        "stop_hosting",
    )
    assert capabilities.supported_event_types == (
        "session_started",
        "observation_updated",
        "skill_started",
        "skill_finished",
        "decision_rejected",
        "session_stopped",
        "chat_received",
        "nearby_friend_chat_requested",
        "chat_send_result",
    )

    with pytest.raises(TypeError):
        capabilities.supported_decision_actions[0] = "wait"
    with pytest.raises(TypeError):
        capabilities.supported_event_types[0] = "observation_updated"


def test_model_accepts_only_camel_case_external_fields() -> None:
    capabilities = GatewayV2Capabilities.model_validate(EXPECTED_CAPABILITIES)

    assert capabilities.max_event_batch_size == 64
    assert capabilities.max_decision_ttl_ms == 30_000


@pytest.mark.parametrize(
    "invalid_input",
    [
        {**EXPECTED_CAPABILITIES, "unexpected": "field"},
        {
            **EXPECTED_CAPABILITIES,
            "max_event_batch_size": 64,
        },
        {
            key: value
            for key, value in EXPECTED_CAPABILITIES.items()
            if key != "maxEventBatchSize"
        }
        | {"max_event_batch_size": 64},
    ],
    ids=["extra-field", "snake-case-extra", "snake-case-instead-of-alias"],
)
def test_model_rejects_extra_and_snake_case_external_fields(
    invalid_input: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GatewayV2Capabilities.model_validate(invalid_input)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("contractVersion", "llm-gateway-http-v1"),
        ("receiveEventsPath", "/api/gateway/v2/other"),
        (
            "supportedDecisionActions",
            ["wait", "call_skill", "no_op", "stop_hosting"],
        ),
        ("perEventAck", False),
        ("controlGeneration", False),
        ("eventSequence", False),
        ("asyncSkillTerminal", False),
        (
            "supportedEventTypes",
            [
                "observation_updated",
                "session_started",
                "skill_started",
                "skill_finished",
                "decision_rejected",
                "session_stopped",
            ],
        ),
    ],
)
def test_model_rejects_changes_to_fixed_capabilities(
    field: str,
    invalid_value: object,
) -> None:
    invalid_input = deepcopy(EXPECTED_CAPABILITIES)
    invalid_input[field] = invalid_value

    with pytest.raises(ValidationError):
        GatewayV2Capabilities.model_validate(invalid_input)


@pytest.mark.parametrize(
    "field",
    ["perEventAck", "controlGeneration", "eventSequence", "asyncSkillTerminal"],
)
@pytest.mark.parametrize("invalid_value", [1, "true"])
def test_model_rejects_non_strict_true_capability_flags(
    field: str,
    invalid_value: object,
) -> None:
    invalid_input = deepcopy(EXPECTED_CAPABILITIES)
    invalid_input[field] = invalid_value

    with pytest.raises(ValidationError):
        GatewayV2Capabilities.model_validate(invalid_input)


@pytest.mark.parametrize("field", ["maxEventBatchSize", "maxDecisionTtlMs"])
@pytest.mark.parametrize("invalid_value", [0, -1, True, False, 1.0, "1"])
def test_model_rejects_non_strict_positive_integer_limits(
    field: str,
    invalid_value: object,
) -> None:
    invalid_input = deepcopy(EXPECTED_CAPABILITIES)
    invalid_input[field] = invalid_value

    with pytest.raises(ValidationError):
        GatewayV2Capabilities.model_validate(invalid_input)
