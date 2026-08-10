from copy import deepcopy

import pytest

from src.core.integration.llm_gateway_v2.canonical import canonical_event_bytes, event_content_hash
from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchEnvelope, parse_gateway_v2_event


def _lease() -> dict:
    return {
        "sessionId": "session-1",
        "controlGeneration": 1,
        "decisionLeaseId": "lease-1",
        "stateVersion": 1,
        "leaseKind": "hosting_control",
        "allowedActions": ["wait"],
        "allowedSkillName": None,
        "allowedSkillNames": [],
        "parentSkillName": None,
    }


def _decision_payload(*, lease: dict | None = None) -> dict:
    return {
        "reason": "decision_requested",
        "lease": lease or _lease(),
        "decisionContext": {
            "session": {"status": "active", "position": {"x": 1, "y": 2}},
            "availableSkills": [],
            "skillArgumentHints": [],
            "lastSkillResult": None,
        },
    }


def _event(**overrides: object) -> dict:
    value: dict[str, object] = {
        "eventId": "event-1",
        "eventType": "session_started",
        "sessionId": "session-1",
        "controlGeneration": 1,
        "eventSequence": 1,
        "stateVersion": 1,
        "decisionLeaseId": "lease-1",
        "occurredAtMs": 1_700_000_000_000,
        "payload": _decision_payload(),
    }
    value.update(overrides)
    return value


def _envelope(*, trace_id: str, sent_at_ms: int, event: dict) -> GatewayV2BatchEnvelope:
    return GatewayV2BatchEnvelope.model_validate(
        {
            "traceId": trace_id,
            "gatewayId": "gateway-1",
            "contractVersion": "llm-gateway-http-v2",
            "sentAtMs": sent_at_ms,
            "events": [event],
        }
    )


def test_hash_excludes_envelope_and_hmac_transport_metadata() -> None:
    first = _envelope(trace_id="trace-first", sent_at_ms=1, event=_event())
    retry = _envelope(trace_id="trace-retry", sent_at_ms=9_999, event=_event())

    assert event_content_hash("gateway-1", first.events[0]) == event_content_hash("gateway-1", retry.events[0])
    canonical = canonical_event_bytes(first.events[0])
    assert b"traceId" not in canonical
    assert b"sentAtMs" not in canonical
    assert b"requestId" not in canonical


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "stateVersion": 2,
            "payload": _decision_payload(lease={**_lease(), "stateVersion": 2}),
        },
        {
            "controlGeneration": 2,
            "payload": _decision_payload(
                lease={**_lease(), "controlGeneration": 2}
            ),
        },
        {
            "eventType": "observation_updated",
            "eventSequence": 2,
            "payload": _decision_payload(),
        },
    ],
)
def test_hash_changes_when_business_event_content_changes(mutation: dict) -> None:
    original = parse_gateway_v2_event(_event())
    changed_payload = deepcopy(_event())
    changed_payload.update(mutation)
    changed = parse_gateway_v2_event(changed_payload)

    assert event_content_hash("gateway-1", original) != event_content_hash("gateway-1", changed)


def test_canonical_bytes_are_deterministic_compact_utf8_json() -> None:
    first_payload = _event()
    second_payload = {
        "payload": first_payload["payload"],
        "occurredAtMs": first_payload["occurredAtMs"],
        "eventSequence": first_payload["eventSequence"],
        "controlGeneration": first_payload["controlGeneration"],
        "stateVersion": first_payload["stateVersion"],
        "decisionLeaseId": first_payload["decisionLeaseId"],
        "sessionId": first_payload["sessionId"],
        "eventType": first_payload["eventType"],
        "eventId": first_payload["eventId"],
    }

    first = canonical_event_bytes(parse_gateway_v2_event(first_payload))
    second = canonical_event_bytes(parse_gateway_v2_event(second_payload))

    assert first == second
    assert b" " not in first
    assert first.decode("utf-8").startswith('{"controlGeneration":')
    assert len(event_content_hash("gateway-1", parse_gateway_v2_event(first_payload))) == 64


def test_hash_binds_gateway_id() -> None:
    event = parse_gateway_v2_event(_event())

    assert event_content_hash("gateway-1", event) != event_content_hash("gateway-2", event)
