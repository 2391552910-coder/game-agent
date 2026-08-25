from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.middleware import PUBLIC_PATHS
from src.api.routes import monitor
from src.core.integration.llm_gateway_v2.monitor_repository import MonitorPage


class FakeMonitorRepository:
    def __init__(self, pages: list[MonitorPage]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    async def list_records(self, **kwargs: object) -> MonitorPage:
        self.calls.append(kwargs)
        if self.pages:
            return self.pages.pop(0)
        return MonitorPage(records=[], next_cursor=kwargs.get("cursor"), has_more=False)


def _record(record_id: str = "event:event-1") -> dict[str, object]:
    return {
        "id": record_id,
        "kind": "event",
        "status": "succeeded",
        "occurredAt": "2026-08-24T08:15:30.000000Z",
        "gatewayId": "gateway-main",
        "sessionId": "session-1",
        "title": "observation_updated",
        "request": {"eventId": "event-1"},
        "response": None,
        "error": None,
        "tokenUsage": None,
    }


@pytest.fixture
def monitor_app() -> FastAPI:
    app = FastAPI()
    app.include_router(monitor.router)
    return app


@pytest.mark.asyncio
async def test_monitor_list_is_public_and_forwards_filters(monkeypatch, monitor_app: FastAPI) -> None:
    repository = FakeMonitorRepository(
        [MonitorPage(records=[_record()], next_cursor="next-page", has_more=True, record_cursors=("stream-cursor",))]
    )
    monkeypatch.setattr(monitor, "get_monitor_repository", lambda: repository)

    async with AsyncClient(transport=ASGITransport(app=monitor_app), base_url="http://test") as client:
        response = await client.get(
            "/api/gateway/v2/monitor",
            params={
                "limit": 25,
                "cursor": "older-page",
                "kind": "event",
                "status": "succeeded",
                "session_id": "session-1",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "records": [_record()],
        "nextCursor": "next-page",
        "hasMore": True,
        "streamCursor": "stream-cursor",
    }
    assert repository.calls == [
        {
            "limit": 25,
            "cursor": "older-page",
            "kind": "event",
            "status": "succeeded",
            "session_id": "session-1",
            "direction": "older",
        }
    ]
    assert "/api/gateway/v2/monitor" in PUBLIC_PATHS
    assert "/api/gateway/v2/monitor/stream" in PUBLIC_PATHS
    assert "/monitor" not in PUBLIC_PATHS


@pytest.mark.asyncio
async def test_monitor_list_rejects_out_of_range_limit(monitor_app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=monitor_app), base_url="http://test") as client:
        response = await client.get("/api/gateway/v2/monitor", params={"limit": 201})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_monitor_list_rejects_invalid_cursor_without_querying_database(monitor_app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=monitor_app), base_url="http://test") as client:
        response = await client.get("/api/gateway/v2/monitor", params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid monitor cursor or filter"}


class DisconnectingRequest:
    def __init__(self, disconnect_after: int) -> None:
        self.headers: dict[str, str] = {}
        self._checks = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks >= self._disconnect_after


@pytest.mark.asyncio
async def test_monitor_stream_replays_records_with_sse_ids() -> None:
    repository = FakeMonitorRepository(
        [MonitorPage(records=[_record()], next_cursor="live-cursor", has_more=False)]
    )
    request = DisconnectingRequest(disconnect_after=2)

    chunks = [
        chunk
        async for chunk in monitor.iter_monitor_events(
            request,
            repository,
            cursor="resume-cursor",
            poll_interval_seconds=0,
            heartbeat_seconds=30,
        )
    ]

    assert len(chunks) == 1
    assert chunks[0].startswith("id: live-cursor\nevent: record\ndata: ")
    payload = json.loads(chunks[0].split("data: ", 1)[1])
    assert payload == _record()
    assert repository.calls[0]["cursor"] == "resume-cursor"
    assert repository.calls[0]["direction"] == "newer"


@pytest.mark.asyncio
async def test_monitor_stream_emits_heartbeat_when_idle(monkeypatch) -> None:
    now = datetime(2026, 8, 24, 8, 15, 30, tzinfo=UTC)
    repository = FakeMonitorRepository([])
    request = DisconnectingRequest(disconnect_after=2)
    monkeypatch.setattr(monitor, "utc_now", lambda: now)

    chunks = [
        chunk
        async for chunk in monitor.iter_monitor_events(
            request,
            repository,
            cursor=None,
            poll_interval_seconds=0,
            heartbeat_seconds=0,
        )
    ]

    assert chunks == [": heartbeat 2026-08-24T08:15:30+00:00\n\n"]


@pytest.mark.asyncio
async def test_monitor_stream_stops_cleanly_when_cancelled() -> None:
    repository = FakeMonitorRepository([])
    request = DisconnectingRequest(disconnect_after=10_000)
    stream = monitor.iter_monitor_events(
        request,
        repository,
        cursor=None,
        poll_interval_seconds=60,
        heartbeat_seconds=60,
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()
