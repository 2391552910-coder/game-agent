from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.infrastructure.db import async_session_factory


@dataclass(frozen=True)
class MonitorRecord:
    record_type: str
    direction: str
    status: str | None = None
    tenant_id: UUID | None = None
    gateway_id: str | None = None
    session_id: str | None = None
    event_id: str | None = None
    trace_id: str | None = None
    decision_id: str | None = None
    occurred_at: datetime | None = None
    request_body_json: Mapping[str, Any] | None = None
    response_body_json: Mapping[str, Any] | None = None
    content: str | None = None
    error_stage: str | None = None
    error_category: str | None = None
    error_detail: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    model_calls: int | None = None
    usage_reported_calls: int | None = None
    usage_missing_calls: int | None = None


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_APPEND = sa.text(
    """
    INSERT INTO llm_gateway_monitor_records (
        tenant_id, gateway_id, session_id, event_id, trace_id, decision_id,
        record_type, direction, occurred_at, status, request_body_json,
        response_body_json, content, error_stage, error_category, error_detail,
        input_tokens, output_tokens, total_tokens, model_calls,
        usage_reported_calls, usage_missing_calls
    ) VALUES (
        :tenant_id, :gateway_id, :session_id, :event_id, :trace_id, :decision_id,
        :record_type, :direction, COALESCE(:occurred_at, clock_timestamp()), :status,
        :request_body_json, :response_body_json, :content, :error_stage,
        :error_category, :error_detail, :input_tokens, :output_tokens,
        :total_tokens, :model_calls, :usage_reported_calls, :usage_missing_calls
    )
    RETURNING id
    """
).bindparams(
    sa.bindparam("request_body_json", type_=JSONB),
    sa.bindparam("response_body_json", type_=JSONB),
)

_LIST_AFTER = sa.text(
    """
    SELECT id, tenant_id, gateway_id, session_id, event_id, trace_id, decision_id,
           record_type, direction, occurred_at, status, request_body_json,
           response_body_json, content, error_stage, error_category, error_detail,
           input_tokens, output_tokens, total_tokens, model_calls,
           usage_reported_calls, usage_missing_calls, created_at, updated_at
    FROM llm_gateway_monitor_records
    WHERE id > :cursor
      AND (:gateway_id IS NULL OR gateway_id = :gateway_id)
    ORDER BY id
    LIMIT :limit
    """
)


class MonitorAuditRepository:
    def __init__(self, session_factory: _SessionFactory = async_session_factory) -> None:
        self._session_factory = session_factory

    async def append(self, record: MonitorRecord) -> int:
        params = {
            "tenant_id": record.tenant_id,
            "gateway_id": record.gateway_id,
            "session_id": record.session_id,
            "event_id": record.event_id,
            "trace_id": record.trace_id,
            "decision_id": record.decision_id,
            "record_type": record.record_type,
            "direction": record.direction,
            "occurred_at": record.occurred_at,
            "status": record.status,
            "request_body_json": dict(record.request_body_json) if record.request_body_json is not None else None,
            "response_body_json": dict(record.response_body_json) if record.response_body_json is not None else None,
            "content": record.content,
            "error_stage": record.error_stage,
            "error_category": record.error_category,
            "error_detail": record.error_detail[:512] if record.error_detail is not None else None,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "total_tokens": record.total_tokens,
            "model_calls": record.model_calls,
            "usage_reported_calls": record.usage_reported_calls,
            "usage_missing_calls": record.usage_missing_calls,
        }
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(_APPEND, params)
                value = result.scalar_one()
                if value is None:
                    raise RuntimeError("monitor audit insert returned no id")
                return int(value)
        except SQLAlchemyError:
            raise

    async def list_after(
        self,
        cursor: int = 0,
        *,
        limit: int = 100,
        gateway_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        if cursor < 0 or limit <= 0:
            raise ValueError("cursor must be non-negative and limit must be positive")
        async with self._session_factory() as session:
            result = await session.execute(
                _LIST_AFTER,
                {"cursor": cursor, "limit": min(limit, 1_000), "gateway_id": gateway_id},
            )
            return tuple(result.mappings().all())

    async def record_hosted_chat(self, item: Mapping[str, object]) -> None:
        request_body = item.get("request_body_json")
        if not isinstance(request_body, Mapping):
            content = _optional_string(item.get("content"))
            request_body = {"content": content} if content else None
        await self.append(
            MonitorRecord(
                gateway_id=_optional_string(item.get("gateway_id")),
                session_id=_optional_string(item.get("session_id")),
                event_id=_optional_string(item.get("event_id")),
                record_type="chat",
                direction=str(item.get("direction", "system")),
                status=_optional_string(item.get("status")),
                content=_optional_string(item.get("content")),
                request_body_json=request_body,
                response_body_json={
                    key: value
                    for key in ("request_id", "chat_message_id")
                    if (value := item.get(key)) is not None
                }
                or None,
                error_stage="chat" if item.get("error_category") is not None else None,
                error_category=_optional_string(item.get("error_category")),
            )
        )


async def safe_record(
    recorder: MonitorAuditRepository | None,
    record: MonitorRecord,
) -> None:
    """Best-effort audit write; monitoring must not break Gateway processing."""
    if recorder is None:
        return
    try:
        await recorder.append(record)
    except Exception:
        return


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
