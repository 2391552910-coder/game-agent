"""RobotGateway HTTP 数据源测试。"""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from src.game_specific import connector


@pytest.mark.asyncio
async def test_fetch_robotgateway_snapshot_gets_player_snapshot_with_api_key():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "user_id": "player_001",
                "player_name": "模拟玩家_player_001",
                "level": 28,
                "game_specific": {"current_area": "商业区"},
            },
        )

    snapshot = await connector._fetch_robotgateway_snapshot(
        user_id="player_001",
        base_url="http://robotgateway.local",
        api_key="secret",
        timeout_seconds=3.0,
        transport=httpx.MockTransport(handler),
    )

    assert snapshot["user_id"] == "player_001"
    assert snapshot["level"] == 28
    assert snapshot["game_specific"]["current_area"] == "商业区"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://robotgateway.local/players/player_001/snapshot"
    assert requests[0].headers["X-API-Key"] == "secret"


@pytest.mark.asyncio
async def test_fetch_robotgateway_snapshot_raises_database_error_on_http_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(connector.DatabaseError):
        await connector._fetch_robotgateway_snapshot(
            user_id="player_001",
            base_url="http://robotgateway.local",
            api_key=None,
            timeout_seconds=3.0,
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_fetch_player_snapshot_uses_robotgateway_mode(monkeypatch):
    fetch_mock = AsyncMock(return_value={"user_id": "player_001", "level": 28})
    monkeypatch.setenv("GAME_DATA_SOURCE", "robotgateway")
    monkeypatch.setenv("ROBOTGATEWAY_BASE_URL", "http://robotgateway.local")
    monkeypatch.setenv("ROBOTGATEWAY_SNAPSHOT_API_KEY", "secret")
    monkeypatch.setenv("ROBOTGATEWAY_SNAPSHOT_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setattr(connector, "_fetch_robotgateway_snapshot", fetch_mock)

    snapshot = await connector.fetch_player_snapshot("player_001")

    assert snapshot == {"user_id": "player_001", "level": 28}
    fetch_mock.assert_awaited_once_with(
        user_id="player_001",
        base_url="http://robotgateway.local",
        api_key="secret",
        timeout_seconds=7.5,
    )


def _load_mock_robotgateway_server():
    server_path = Path(__file__).resolve().parents[2] / "模拟服务端" / "server.py"
    spec = importlib.util.spec_from_file_location("mock_robotgateway_server", server_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_mock_robotgateway_server_returns_snapshot():
    server = _load_mock_robotgateway_server()
    transport = ASGITransport(app=server.app)

    async with AsyncClient(transport=transport, base_url="http://mock-robotgateway") as client:
        response = await client.get("/players/player_001/snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "player_001"
    assert body["player_name"] == "模拟玩家_player_001"
    assert body["game_specific"]["source"] == "mock_robotgateway"


@pytest.mark.asyncio
async def test_mock_robotgateway_server_prints_received_callback(capsys):
    server = _load_mock_robotgateway_server()
    transport = ASGITransport(app=server.app)
    payload = {
        "event_type": "analysis.completed",
        "tenant_id": "tenant_001",
        "user_id": "player_001",
        "analysis": {"final_output": "去商业区学习编程课程"},
    }

    async with AsyncClient(transport=transport, base_url="http://mock-robotgateway") as client:
        response = await client.post("/callbacks/analysis", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "received", "user_id": "player_001"}
    captured = capsys.readouterr()
    assert "RobotGateway received analysis callback" in captured.out
    assert "player_001" in captured.out
