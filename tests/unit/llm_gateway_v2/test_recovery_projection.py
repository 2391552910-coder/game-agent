from __future__ import annotations

import pytest

from src.core.integration.llm_gateway_v2.recovery import (
    EventFingerprintRegistry,
    RecoveryConsistencyError,
    RecoveryProjection,
)


def test_recovery_projection_rebuilds_event_fingerprints_and_terminal_links() -> None:
    projection = RecoveryProjection.from_rows(
        event_rows=[
            {
                "gateway_id": "gateway-1",
                "event_id": "event-1",
                "content_hash": "a" * 64,
                "event_type": "skill_finished",
                "session_id": "session-1",
                "control_generation": 1,
                "status": "succeeded",
            }
        ],
        decision_rows=[
            {
                "gateway_id": "gateway-1",
                "decision_id": "decision-1",
                "session_id": "session-1",
                "control_generation": 1,
                "status": "accepted",
            }
        ],
        skill_call_rows=[
            {
                "gateway_id": "gateway-1",
                "skill_call_id": "call-1",
                "decision_id": "decision-1",
                "session_id": "session-1",
                "terminal_event_id": "event-1",
                "status": "succeeded",
            }
        ],
    )

    assert projection.fingerprints.matches("gateway-1", "event-1", "a" * 64)
    assert projection.fingerprints.is_seen("gateway-1", "event-1")
    assert projection.terminal_by_skill_call["gateway-1:call-1"].terminal_event_id == "event-1"
    assert projection.decision_ids == ("decision-1",)


def test_replayed_event_with_changed_content_is_not_treated_as_idempotent() -> None:
    registry = EventFingerprintRegistry({("gateway-1", "event-1"): "a" * 64})

    assert not registry.matches("gateway-1", "event-1", "b" * 64)
    assert registry.is_seen("gateway-1", "event-1")


def test_recovery_rejects_terminal_link_without_original_event() -> None:
    with pytest.raises(RecoveryConsistencyError, match="event-1"):
        RecoveryProjection.from_rows(
            event_rows=[],
            decision_rows=[],
            skill_call_rows=[
                {
                    "gateway_id": "gateway-1",
                    "skill_call_id": "call-1",
                    "decision_id": "decision-1",
                    "session_id": "session-1",
                    "terminal_event_id": "event-1",
                    "status": "succeeded",
                }
            ],
        )


def test_recovery_preserves_pending_skill_call_without_terminal_event() -> None:
    projection = RecoveryProjection.from_rows(
        event_rows=[],
        decision_rows=[
            {
                "gateway_id": "gateway-1",
                "decision_id": "decision-1",
                "session_id": "session-1",
                "control_generation": 1,
                "status": "accepted",
            }
        ],
        skill_call_rows=[
            {
                "gateway_id": "gateway-1",
                "skill_call_id": "call-1",
                "decision_id": "decision-1",
                "session_id": "session-1",
                "terminal_event_id": None,
                "status": "pending",
            }
        ],
    )

    assert projection.terminal_by_skill_call["gateway-1:call-1"].terminal_event_id is None
