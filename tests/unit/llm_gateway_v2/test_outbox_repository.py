from __future__ import annotations

import pytest

from src.core.integration.llm_gateway_v2.outbox_repository import decision_lease_deadline_ms


def test_decision_lease_deadline_reserves_safety_window() -> None:
    assert decision_lease_deadline_ms(
        occurred_at_ms=1_700_000_000_000,
        lease_ttl_ms=600_000,
        safety_window_ms=5_000,
    ) == 1_700_000_595_000


@pytest.mark.parametrize(
    ("lease_ttl_ms", "safety_window_ms"),
    [(0, 0), (1_000, -1), (1_000, 1_000), (1_000, 2_000)],
)
def test_decision_lease_deadline_rejects_invalid_configuration(
    lease_ttl_ms: int,
    safety_window_ms: int,
) -> None:
    with pytest.raises(ValueError):
        decision_lease_deadline_ms(
            occurred_at_ms=1_700_000_000_000,
            lease_ttl_ms=lease_ttl_ms,
            safety_window_ms=safety_window_ms,
        )
