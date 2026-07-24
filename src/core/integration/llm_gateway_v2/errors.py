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
    ):
        if value is not None:
            fields[name] = value
    if elapsed_ms is not None:
        fields["elapsed_ms"] = elapsed_ms
    return fields
