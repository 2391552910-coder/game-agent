from __future__ import annotations

from typing import Any


class GatewayV2OperationalError(Exception):
    def __init__(
        self,
        *,
        stage: str,
        category: str,
        retryable: bool,
    ) -> None:
        self.stage = stage
        self.category = category
        self.retryable = retryable
        super().__init__("gateway v2 operation failed")


def safe_exception_fields(
    *,
    stage: str,
    category: str,
    error: BaseException,
    trace_id: str | None = None,
    event_id: str | None = None,
    decision_id: str | None = None,
    skill_call_id: str | None = None,
    session_id: str | None = None,
    decision_lease_id: str | None = None,
    control_generation: int | None = None,
    state_version: int | None = None,
    http_status: int | None = None,
    response_status: str | None = None,
    response_reason: str | None = None,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "stage": stage,
        "error_category": category,
        "exception_type": type(error).__name__,
    }
    for name, value in (
        ("trace_id", trace_id),
        ("event_id", event_id),
        ("decision_id", decision_id),
        ("skill_call_id", skill_call_id),
        ("session_id", session_id),
        ("decision_lease_id", decision_lease_id),
    ):
        if value is not None:
            fields[name] = value
    for name, value in (
        ("control_generation", control_generation),
        ("state_version", state_version),
        ("http_status", http_status),
    ):
        if value is not None:
            fields[name] = value
    if response_status is not None:
        fields["response_status"] = response_status
    if response_reason is not None:
        fields["response_reason"] = response_reason[:256]
    if elapsed_ms is not None:
        fields["elapsed_ms"] = elapsed_ms
    return fields
