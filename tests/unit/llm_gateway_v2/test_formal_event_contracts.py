import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchEnvelope

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "llm_gateway_v2"
    / "formal_non_chat_events.json"
)


def _skill_descriptor(name: str = "move_to") -> dict[str, Any]:
    return {
        "SkillName": name,
        "SchemaVersion": "v1",
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


def _argument_field(path: str, *, status: str = "allowed") -> dict[str, Any]:
    return {
        "path": path,
        "type": "number",
        "status": status,
        "source": "contract",
        "statePath": None,
        "reason": None,
        "nextStep": None,
    }


def _decision_context() -> dict[str, Any]:
    return {
        "session": {
            "accountId": "account-1",
            "status": "running",
            "ExecutingSkillName": None,
        },
        "availableSkills": [_skill_descriptor()],
        "skillArgumentHints": [
            {
                "skillName": "move_to",
                "schemaVersion": "v1",
                "argumentStatus": "ready",
                "suggestedArgs": {},
                "allowedArgs": [_argument_field("target.x")],
                "missingArgs": [
                    _argument_field("target.x", status="missing")
                ],
                "warnings": [],
                "nextSteps": [],
            }
        ],
        "lastSkillResult": None,
    }


def _lease() -> dict[str, Any]:
    return {
        "sessionId": "session-1",
        "controlGeneration": 1,
        "stateVersion": 7,
        "decisionLeaseId": "lease-1",
        "leaseKind": "observation",
        "allowedActions": ["call_skill", "wait", "no_op"],
        "allowedSkillName": "move_to",
        "allowedSkillNames": ["move_to"],
        "parentSkillName": None,
    }


def _event(
    event_type: str,
    *,
    sequence: int,
    payload: dict[str, Any],
    decision_lease_id: str | None = None,
) -> dict[str, Any]:
    return {
        "eventId": f"event-{sequence}",
        "eventType": event_type,
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": sequence,
        "stateVersion": 7,
        "decisionLeaseId": decision_lease_id,
        "occurredAtMs": 1_700_000_000_000 + sequence,
        "payload": payload,
    }


def _formal_events() -> list[dict[str, Any]]:
    decision_payload = {
        "reason": "decision_requested",
        "lease": _lease(),
        "decisionContext": _decision_context(),
    }
    return [
        _event(
            "session_started",
            sequence=1,
            payload=deepcopy(decision_payload),
            decision_lease_id="lease-1",
        ),
        _event(
            "observation_updated",
            sequence=2,
            payload=deepcopy(decision_payload),
            decision_lease_id="lease-1",
        ),
        _event(
            "skill_started",
            sequence=3,
            payload={
                "decisionId": "decision-1",
                "skillName": "move_to",
                "skillCallId": "call-1",
                "startedAtMs": 1_700_000_000_003,
            },
        ),
        _event(
            "skill_finished",
            sequence=4,
            payload={
                "decisionId": "decision-1",
                "skillName": "move_to",
                "skillCallId": "call-1",
                "status": "success",
                "reason": "ok",
                "failureCategory": None,
                "retryable": False,
                "startedAtMs": 1_700_000_000_003,
                "finishedAtMs": 1_700_000_000_004,
                "lease": deepcopy(_lease()),
                "decisionContext": _decision_context(),
            },
            decision_lease_id="lease-1",
        ),
        _event(
            "decision_rejected",
            sequence=5,
            payload={
                "decisionId": "decision-2",
                "action": "call_skill",
                "skillName": "move_to",
                "reason": "stale_state",
                "rejectedAtMs": 1_700_000_000_005,
            },
        ),
        _event(
            "session_stopped",
            sequence=6,
            payload={
                "reason": "hosting_stopped",
                "stoppedAtMs": 1_700_000_000_006,
            },
        ),
    ]


def _batch(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "traceId": "trace-1",
        "gatewayId": "gateway-1",
        "contractVersion": "llm-gateway-http-v2",
        "sentAtMs": 1_700_000_000_100,
        "events": events,
    }


def test_gateway_formal_non_chat_events_parse_without_shape_conversion() -> None:
    events = _formal_events()

    parsed = GatewayV2BatchEnvelope.model_validate(_batch(events))

    assert parsed.model_dump(mode="json") == _batch(events)


def test_remediation_spec_simulation_fixture_covers_non_chat_contract() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    events = fixture["events"]
    batch = _batch(events)
    batch["contractVersion"] = fixture["contractVersion"]

    parsed = GatewayV2BatchEnvelope.model_validate(batch)

    assert fixture["fixtureType"] == "remediation_spec_simulation"
    assert fixture["realGatewayExport"] is False
    assert fixture["contractSource"] == "docs/llm-remediation-technical.md"
    assert list(dict.fromkeys(event.event_type for event in parsed.events)) == [
        "session_started",
        "observation_updated",
        "skill_started",
        "skill_finished",
        "decision_rejected",
        "session_stopped",
    ]
    lease_kinds = {
        event.payload.lease.lease_kind
        for event in parsed.events
        if hasattr(event.payload, "lease") and event.payload.lease is not None
    }
    assert lease_kinds == {
        "observation",
        "movement_control",
        "vehicle_cancel_window",
        "vehicle_recovery",
    }
    assert parsed.model_dump(mode="json", exclude_unset=True)["events"] == events


@pytest.mark.parametrize("field", ["stateVersion", "decisionLeaseId"])
def test_formal_event_root_fields_are_required(field: str) -> None:
    event = _formal_events()[0]
    event.pop(field)

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(_batch([event]))


def test_lease_fields_are_not_accepted_at_decision_context_level() -> None:
    event = _formal_events()[0]
    event["payload"]["decisionContext"]["leaseKind"] = event["payload"][
        "lease"
    ].pop("leaseKind")

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(_batch([event]))


def test_skill_finished_rejects_nested_terminal_wrapper() -> None:
    event = _formal_events()[3]
    payload = event["payload"]
    payload["terminal"] = {
        key: payload.pop(key)
        for key in (
            "status",
            "reason",
            "failureCategory",
            "retryable",
            "startedAtMs",
            "finishedAtMs",
        )
    }

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(_batch([event]))


def test_skill_finished_rejects_decision_rejected_status() -> None:
    event = _formal_events()[3]
    event["payload"]["status"] = "rejected"

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(_batch([event]))


def test_formal_contract_keeps_strict_unknown_field_rejection() -> None:
    event = _formal_events()[0]
    event["payload"]["lease"]["unknown"] = True

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(_batch([event]))


@pytest.mark.parametrize(
    ("root_field", "lease_field", "wrong_value"),
    [
        ("sessionId", "sessionId", "another-session"),
        ("controlGeneration", "controlGeneration", 2),
        ("stateVersion", "stateVersion", 8),
        ("decisionLeaseId", "decisionLeaseId", "another-lease"),
    ],
)
def test_event_root_must_match_embedded_lease(
    root_field: str,
    lease_field: str,
    wrong_value: Any,
) -> None:
    event = _formal_events()[0]
    event[root_field] = wrong_value

    with pytest.raises(ValidationError):
        GatewayV2BatchEnvelope.model_validate(_batch([event]))
