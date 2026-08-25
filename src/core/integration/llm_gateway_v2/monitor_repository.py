from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.infrastructure.db import async_session_factory

MonitorDirection = Literal["older", "newer"]


class MonitorCursorError(ValueError):
    pass


@dataclass(frozen=True)
class MonitorCursor:
    occurred_at: datetime
    record_id: str


@dataclass(frozen=True)
class MonitorPage:
    records: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool
    record_cursors: tuple[str, ...] = field(default_factory=tuple)


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_BASE_RECORDS_SQL = """
WITH monitor_records AS (
    SELECT
        'event:' || e.id::text AS record_id,
        'event'::text AS kind,
        'inbound'::text AS direction,
        e.status,
        e.updated_at AS occurred_at,
        e.gateway_id,
        e.session_id,
        e.event_id,
        e.trace_id,
        NULL::text AS decision_id,
        e.event_type,
        e.event_type AS title,
        CASE WHEN e.event_type = 'chat_received' THEN e.event_body #>> '{{payload,text}}' ELSE NULL END AS content,
        e.event_body AS request_body,
        NULL::jsonb AS response_body,
        CASE
            WHEN e.error_stage IS NULL AND e.error_category IS NULL THEN NULL::jsonb
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'stage', e.error_stage,
                'category', e.error_category
            ))
        END AS error_body,
        NULL::jsonb AS token_usage
    FROM llm_gateway_events AS e

    UNION ALL

    SELECT
        'decision:' || d.id::text,
        'decision'::text,
        'outbound'::text,
        d.status,
        d.updated_at,
        d.gateway_id,
        d.session_id,
        source_event.event_id,
        source_event.trace_id,
        d.decision_id,
        NULL::text AS event_type,
        d.action,
        NULL::text,
        d.request_body_json,
        COALESCE(
            d.response_body_json,
            CASE
                WHEN d.response_http_status IS NULL
                     AND d.response_status IS NULL
                     AND d.response_reason IS NULL
                     AND d.skill_call_id IS NULL
                THEN NULL::jsonb
                ELSE jsonb_strip_nulls(jsonb_build_object(
                    'httpStatus', d.response_http_status,
                    'status', d.response_status,
                    'reason', d.response_reason,
                    'skillCallId', d.skill_call_id
                ))
            END
        ),
        CASE
            WHEN d.error_stage IS NULL AND d.error_category IS NULL THEN NULL::jsonb
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'stage', d.error_stage,
                'category', d.error_category,
                'detail', d.response_reason
            ))
        END,
        CASE
            WHEN d.input_tokens IS NULL AND d.output_tokens IS NULL AND d.total_tokens IS NULL
            THEN NULL::jsonb
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'inputTokens', d.input_tokens,
                'outputTokens', d.output_tokens,
                'totalTokens', d.total_tokens,
                'modelCalls', d.model_calls,
                'usageReportedCalls', d.usage_reported_calls,
                'usageMissingCalls', d.usage_missing_calls
            ))
        END
    FROM llm_gateway_decisions AS d
    LEFT JOIN llm_gateway_events AS source_event ON source_event.id = d.source_event_id

    UNION ALL

    SELECT
        'skill:' || sc.id::text,
        'skill'::text,
        'inbound'::text,
        sc.status,
        sc.updated_at,
        sc.gateway_id,
        sc.session_id,
        terminal_event.event_id,
        terminal_event.trace_id,
        sc.decision_id,
        NULL::text AS event_type,
        sc.skill_name,
        NULL::text,
        jsonb_strip_nulls(jsonb_build_object(
            'decisionId', sc.decision_id,
            'skillCallId', sc.skill_call_id,
            'skillName', sc.skill_name
        )),
        jsonb_strip_nulls(jsonb_build_object(
            'status', sc.status,
            'reason', sc.reason,
            'retryable', sc.retryable,
            'effectStatus', sc.effect_status
        )),
        CASE
            WHEN sc.failure_category IS NULL AND sc.reason IS NULL THEN NULL::jsonb
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'stage', 'skill_execution',
                'category', sc.failure_category,
                'detail', sc.reason
            ))
        END,
        NULL::jsonb
    FROM llm_gateway_skill_calls AS sc
    LEFT JOIN llm_gateway_events AS terminal_event ON terminal_event.id = sc.terminal_event_id

    UNION ALL

    SELECT
        'audit:' || a.id::text,
        a.record_type,
        a.direction,
        a.status,
        a.occurred_at,
        a.gateway_id,
        a.session_id,
        a.event_id,
        a.trace_id,
        a.decision_id,
        NULL::text AS event_type,
        CASE
            WHEN a.record_type = 'chat' AND a.direction = 'outbound' THEN '托管 Agent 对话'
            WHEN a.record_type = 'chat' THEN 'Gateway 对话'
            WHEN a.record_type = 'error' THEN 'Gateway 调用错误'
            ELSE a.record_type
        END,
        a.content,
        a.request_body_json,
        a.response_body_json,
        CASE
            WHEN a.error_stage IS NULL
                 AND a.error_category IS NULL
                 AND a.error_detail IS NULL
            THEN NULL::jsonb
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'stage', a.error_stage,
                'category', a.error_category,
                'detail', a.error_detail
            ))
        END,
        CASE
            WHEN a.input_tokens IS NULL
                 AND a.output_tokens IS NULL
                 AND a.total_tokens IS NULL
            THEN NULL::jsonb
            ELSE jsonb_strip_nulls(jsonb_build_object(
                'inputTokens', a.input_tokens,
                'outputTokens', a.output_tokens,
                'totalTokens', a.total_tokens,
                'modelCalls', a.model_calls,
                'usageReportedCalls', a.usage_reported_calls,
                'usageMissingCalls', a.usage_missing_calls
            ))
        END
    FROM llm_gateway_monitor_records AS a
    WHERE (
        (a.record_type = 'chat' AND a.direction = 'outbound')
        OR (
            a.record_type = 'error'
            AND (
                a.decision_id IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM llm_gateway_decisions AS existing_decision
                    WHERE existing_decision.gateway_id = a.gateway_id
                      AND existing_decision.decision_id = a.decision_id
                )
            )
        )
    )
)
SELECT
    record_id, kind, direction, status, occurred_at, gateway_id, session_id,
    event_id, trace_id, decision_id, event_type, title, content, request_body,
    response_body, error_body, token_usage
FROM monitor_records
WHERE (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT))
  AND (CAST(:status AS TEXT) IS NULL OR status = CAST(:status AS TEXT))
  AND (CAST(:session_id AS TEXT) IS NULL OR session_id = CAST(:session_id AS TEXT))
  AND (
      CAST(:cursor_at AS TIMESTAMPTZ) IS NULL
      OR {cursor_comparison}
  )
ORDER BY occurred_at {order}, record_id {order}
LIMIT :fetch_limit
"""


def encode_monitor_cursor(cursor: MonitorCursor) -> str:
    occurred_at = cursor.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    payload = json.dumps(
        {"at": occurred_at.astimezone(UTC).isoformat(), "id": cursor.record_id},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_monitor_cursor(value: str) -> MonitorCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
        occurred_at = datetime.fromisoformat(payload["at"])
        record_id = payload["id"]
        if occurred_at.tzinfo is None or not isinstance(record_id, str) or not record_id:
            raise ValueError
        return MonitorCursor(occurred_at=occurred_at.astimezone(UTC), record_id=record_id)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MonitorCursorError("invalid monitor cursor") from error


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _mapping_to_record(row: Mapping[str, Any]) -> dict[str, Any]:
    occurred_at = row["occurred_at"]
    if not isinstance(occurred_at, datetime):
        raise TypeError("monitor occurred_at must be a datetime")
    return {
        "id": str(row["record_id"]),
        "kind": str(row["kind"]),
        "direction": str(row["direction"]),
        "status": None if row["status"] is None else str(row["status"]),
        "occurredAt": _isoformat(occurred_at),
        "gatewayId": None if row["gateway_id"] is None else str(row["gateway_id"]),
        "sessionId": None if row["session_id"] is None else str(row["session_id"]),
        "eventId": None if row["event_id"] is None else str(row["event_id"]),
        "traceId": None if row["trace_id"] is None else str(row["trace_id"]),
        "decisionId": None if row["decision_id"] is None else str(row["decision_id"]),
        "eventType": None if row["event_type"] is None else str(row["event_type"]),
        "title": str(row["title"]),
        "content": row["content"],
        "request": row["request_body"],
        "response": row["response_body"],
        "error": row["error_body"],
        "tokenUsage": row["token_usage"],
    }


class MonitorRepository:
    def __init__(self, session_factory: _SessionFactory = async_session_factory) -> None:
        self._session_factory = session_factory

    async def list_records(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        direction: MonitorDirection = "older",
    ) -> MonitorPage:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if direction not in ("older", "newer"):
            raise ValueError("unsupported monitor direction")

        decoded_cursor = decode_monitor_cursor(cursor) if cursor else None
        is_older = direction == "older"
        comparison = (
            "(occurred_at, record_id) < (CAST(:cursor_at AS TIMESTAMPTZ), CAST(:cursor_id AS TEXT))"
            if is_older
            else "(occurred_at, record_id) > (CAST(:cursor_at AS TIMESTAMPTZ), CAST(:cursor_id AS TEXT))"
        )
        order = "DESC" if is_older else "ASC"
        statement = sa.text(
            _BASE_RECORDS_SQL.format(cursor_comparison=comparison, order=order)
        )
        params = {
            "kind": kind,
            "status": status,
            "session_id": session_id,
            "cursor_at": None if decoded_cursor is None else decoded_cursor.occurred_at,
            "cursor_id": None if decoded_cursor is None else decoded_cursor.record_id,
            "fetch_limit": limit + 1,
        }
        async with self._session_factory() as session:
            result = await session.execute(statement, params)
            rows = list(result.mappings().all())

        has_more = len(rows) > limit
        selected = rows[:limit]
        records = [_mapping_to_record(row) for row in selected]
        record_cursors = tuple(
            encode_monitor_cursor(
                MonitorCursor(
                    occurred_at=row["occurred_at"],
                    record_id=str(row["record_id"]),
                )
            )
            for row in selected
        )
        next_cursor = record_cursors[-1] if record_cursors else cursor
        return MonitorPage(
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
            record_cursors=record_cursors,
        )
