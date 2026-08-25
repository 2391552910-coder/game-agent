from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.core.integration.llm_gateway_v2.monitor_repository import (
    MonitorCursorError,
    MonitorPage,
    MonitorRepository,
    decode_monitor_cursor,
)

router = APIRouter(prefix="/api/gateway/v2/monitor", tags=["gateway-v2-monitor"])


def utc_now() -> datetime:
    return datetime.now(UTC)


_repository = MonitorRepository()


def get_monitor_repository() -> MonitorRepository:
    return _repository


def _public_page(page: MonitorPage) -> dict[str, Any]:
    return {
        "records": page.records,
        "nextCursor": page.next_cursor,
        "hasMore": page.has_more,
        "streamCursor": page.record_cursors[0] if page.record_cursors else None,
    }


@router.get("")
async def list_monitor_records(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=64),
    session_id: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    try:
        page = await get_monitor_repository().list_records(
            limit=limit,
            cursor=cursor,
            kind=kind,
            status=status,
            session_id=session_id,
            direction="older",
        )
    except (MonitorCursorError, ValueError) as error:
        raise HTTPException(status_code=400, detail="invalid monitor cursor or filter") from error
    return _public_page(page)


def _sse_record(record: dict[str, Any], cursor: str | None) -> str:
    event_id = cursor or str(record.get("id", ""))
    return (
        f"id: {event_id}\n"
        "event: record\n"
        f"data: {json.dumps(record, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def iter_monitor_events(
    request: Request,
    repository: MonitorRepository,
    *,
    cursor: str | None,
    kind: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    poll_interval_seconds: float = 1.0,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    current_cursor = cursor
    last_heartbeat = asyncio.get_running_loop().time()
    while not await request.is_disconnected():
        page = await repository.list_records(
            limit=200,
            cursor=current_cursor,
            kind=kind,
            status=status,
            session_id=session_id,
            direction="newer",
        )
        if page.records:
            for index, record in enumerate(page.records):
                record_cursor = page.record_cursors[index] if index < len(page.record_cursors) else page.next_cursor
                if record_cursor:
                    current_cursor = record_cursor
                yield _sse_record(record, record_cursor)
            last_heartbeat = asyncio.get_running_loop().time()
            continue

        now = asyncio.get_running_loop().time()
        if heartbeat_seconds <= 0 or now - last_heartbeat >= heartbeat_seconds:
            yield f": heartbeat {utc_now().isoformat()}\n\n"
            last_heartbeat = now
        if poll_interval_seconds > 0:
            await asyncio.sleep(poll_interval_seconds)
        else:
            await asyncio.sleep(0)


@router.get("/stream")
async def monitor_stream(
    request: Request,
    cursor: str | None = Query(default=None),
    kind: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=64),
    session_id: str | None = Query(default=None, max_length=128),
) -> StreamingResponse:
    resume_cursor = request.headers.get("Last-Event-ID") or cursor
    if resume_cursor is not None:
        try:
            decode_monitor_cursor(resume_cursor)
        except MonitorCursorError as error:
            raise HTTPException(status_code=400, detail="invalid monitor cursor") from error
    generator = iter_monitor_events(
        request,
        get_monitor_repository(),
        cursor=resume_cursor,
        kind=kind,
        status=status,
        session_id=session_id,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
