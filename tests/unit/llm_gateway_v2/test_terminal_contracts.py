import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from src.core.integration.llm_gateway_v2.contracts import (
    SkillFinishedEvent,
    SkillFinishedPayload,
    SkillTerminal,
    SkillTerminalCancelled,
    SkillTerminalFailed,
    SkillTerminalSuccess,
    SkillTerminalTimeout,
    parse_skill_terminal,
)

VALID_TERMINALS: list[tuple[dict[str, Any], type]] = [
    ({"status": "success"}, SkillTerminalSuccess),
    (
        {
            "status": "failed",
            "failureCategory": "business_rejected",
            "reason": "target is blocked",
            "retryable": False,
        },
        SkillTerminalFailed,
    ),
    (
        {
            "status": "cancelled",
            "reason": "session stopped",
            "retryable": False,
        },
        SkillTerminalCancelled,
    ),
    (
        {
            "status": "timeout",
            "reason": "skill deadline exceeded",
            "retryable": True,
        },
        SkillTerminalTimeout,
    ),
]


def skill_finished_event_payload(terminal: dict[str, Any]) -> dict[str, Any]:
    return {
        "eventId": "event-skill-finished",
        "eventType": "skill_finished",
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": 2,
        "occurredAtMs": 1_700_000_000_000,
        "payload": {
            "decisionId": "decision-1",
            "skillCallId": "call-1",
            "terminal": terminal,
        },
    }


@pytest.mark.parametrize(("payload", "model_type"), VALID_TERMINALS)
def test_parse_skill_terminal_returns_concrete_frozen_wire_model(
    payload: dict[str, Any],
    model_type: type,
) -> None:
    terminal = parse_skill_terminal(payload)

    assert type(terminal) is model_type
    assert terminal.model_dump() == payload
    assert json.loads(terminal.model_dump_json()) == payload
    with pytest.raises(ValidationError):
        terminal.status = "changed"


@pytest.mark.parametrize(
    "failure_category",
    [
        "business_rejected",
        "transport_failed",
        "protocol_failed",
        "internal_failed",
    ],
)
def test_failed_accepts_each_failure_category(failure_category: str) -> None:
    payload = {
        "status": "failed",
        "failureCategory": failure_category,
        "reason": "failure reason",
        "retryable": True,
    }

    assert parse_skill_terminal(payload).model_dump() == payload


@pytest.mark.parametrize(
    "failure_category",
    [
        "unknown",
        "",
        None,
        1,
        True,
    ],
)
def test_failed_rejects_unsupported_failure_category(
    failure_category: Any,
) -> None:
    with pytest.raises(ValidationError):
        parse_skill_terminal(
            {
                "status": "failed",
                "failureCategory": failure_category,
                "reason": "failure reason",
                "retryable": False,
            }
        )


@pytest.mark.parametrize("status", ["unknown", "", None, 1, True])
def test_terminal_rejects_invalid_discriminator(status: Any) -> None:
    with pytest.raises(ValidationError):
        parse_skill_terminal({"status": status})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        "success",
        [],
        1,
    ],
)
def test_terminal_rejects_missing_null_and_wrong_root_type(payload: Any) -> None:
    with pytest.raises(ValidationError):
        parse_skill_terminal(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "success", "reason": "not allowed"},
        {"status": "success", "retryable": False},
        {"status": "success", "failureCategory": "internal_failed"},
        {"status": "success", "result": {"moved": True}},
        {
            "status": "cancelled",
            "reason": "cancelled",
            "retryable": False,
            "failureCategory": "business_rejected",
        },
        {
            "status": "timeout",
            "reason": "timed out",
            "retryable": True,
            "failureCategory": "transport_failed",
        },
    ],
)
def test_terminal_variants_reject_fields_owned_by_other_variants(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        parse_skill_terminal(payload)


@pytest.mark.parametrize(
    ("payload", "required_field"),
    [
        (
            {
                "status": "failed",
                "failureCategory": "internal_failed",
                "reason": "failed",
                "retryable": False,
            },
            "failureCategory",
        ),
        (
            {
                "status": "failed",
                "failureCategory": "internal_failed",
                "reason": "failed",
                "retryable": False,
            },
            "reason",
        ),
        (
            {
                "status": "failed",
                "failureCategory": "internal_failed",
                "reason": "failed",
                "retryable": False,
            },
            "retryable",
        ),
        (
            {"status": "cancelled", "reason": "cancelled", "retryable": False},
            "reason",
        ),
        (
            {"status": "cancelled", "reason": "cancelled", "retryable": False},
            "retryable",
        ),
        (
            {"status": "timeout", "reason": "timed out", "retryable": True},
            "reason",
        ),
        (
            {"status": "timeout", "reason": "timed out", "retryable": True},
            "retryable",
        ),
    ],
)
def test_non_success_terminal_requires_all_variant_fields(
    payload: dict[str, Any],
    required_field: str,
) -> None:
    payload.pop(required_field)

    with pytest.raises(ValidationError):
        parse_skill_terminal(payload)


@pytest.mark.parametrize("status", ["failed", "cancelled", "timeout"])
@pytest.mark.parametrize("retryable", [0, 1, "true", None])
def test_non_success_terminal_requires_strict_boolean_retryable(
    status: str,
    retryable: Any,
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "reason": "terminal reason",
        "retryable": retryable,
    }
    if status == "failed":
        payload["failureCategory"] = "internal_failed"

    with pytest.raises(ValidationError):
        parse_skill_terminal(payload)


@pytest.mark.parametrize("status", ["failed", "cancelled", "timeout"])
@pytest.mark.parametrize("reason", [None, 1, "", " \t ", "x" * 257])
def test_non_success_terminal_requires_strict_bounded_nonempty_reason(
    status: str,
    reason: Any,
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "retryable": False,
    }
    if status == "failed":
        payload["failureCategory"] = "internal_failed"

    with pytest.raises(ValidationError):
        parse_skill_terminal(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "failed",
            "failure_category": "internal_failed",
            "reason": "failed",
            "retryable": False,
        },
        {
            "status": "failed",
            "failureCategory": "internal_failed",
            "reason": "failed",
            "retryable": False,
            "extra": "forbidden",
        },
        {
            "status": "cancelled",
            "reason": "cancelled",
            "retryable": False,
            "extra": "forbidden",
        },
        {
            "status": "timeout",
            "reason": "timed out",
            "retryable": True,
            "extra": "forbidden",
        },
    ],
)
def test_terminal_rejects_snake_case_and_extra_fields(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        parse_skill_terminal(payload)


def test_terminal_reason_is_trimmed_on_wire_output() -> None:
    terminal = parse_skill_terminal(
        {
            "status": "timeout",
            "reason": "  skill deadline exceeded  ",
            "retryable": True,
        }
    )

    assert terminal.model_dump() == {
        "status": "timeout",
        "reason": "skill deadline exceeded",
        "retryable": True,
    }


@pytest.mark.parametrize(("payload", "model_type"), VALID_TERMINALS)
def test_skill_finished_event_parses_and_serializes_terminal_union(
    payload: dict[str, Any],
    model_type: type,
) -> None:
    event_payload = skill_finished_event_payload(payload)

    event = SkillFinishedEvent.model_validate(event_payload)

    assert type(event.payload.terminal) is model_type
    assert event.model_dump() == event_payload
    assert json.loads(event.model_dump_json()) == event_payload


def test_skill_finished_payload_rejects_generic_terminal_object() -> None:
    with pytest.raises(ValidationError):
        SkillFinishedPayload.model_validate(
            {
                "decisionId": "decision-1",
                "skillCallId": "call-1",
                "terminal": {"status": "success", "result": {"moved": True}},
            }
        )


def test_skill_terminal_json_schema_has_exact_discriminator_and_one_of() -> None:
    schema = TypeAdapter(SkillTerminal).json_schema()

    assert schema["discriminator"] == {
        "mapping": {
            "cancelled": "#/$defs/SkillTerminalCancelled",
            "failed": "#/$defs/SkillTerminalFailed",
            "success": "#/$defs/SkillTerminalSuccess",
            "timeout": "#/$defs/SkillTerminalTimeout",
        },
        "propertyName": "status",
    }
    assert schema["oneOf"] == [
        {"$ref": "#/$defs/SkillTerminalSuccess"},
        {"$ref": "#/$defs/SkillTerminalFailed"},
        {"$ref": "#/$defs/SkillTerminalCancelled"},
        {"$ref": "#/$defs/SkillTerminalTimeout"},
    ]


def test_skill_finished_payload_schema_embeds_terminal_discriminator() -> None:
    terminal_schema = SkillFinishedPayload.model_json_schema()["properties"][
        "terminal"
    ]

    assert terminal_schema["discriminator"]["propertyName"] == "status"
    assert terminal_schema["oneOf"] == [
        {"$ref": "#/$defs/SkillTerminalSuccess"},
        {"$ref": "#/$defs/SkillTerminalFailed"},
        {"$ref": "#/$defs/SkillTerminalCancelled"},
        {"$ref": "#/$defs/SkillTerminalTimeout"},
    ]
