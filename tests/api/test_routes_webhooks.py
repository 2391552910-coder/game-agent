"""Webhook 路由测试。"""

import json
from unittest.mock import AsyncMock, patch

import pytest


def _auth_headers():
    return {"X-API-Key": "valid-key"}


def _setup_auth(mock_redis):
    tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
    mock_redis.get = AsyncMock(return_value=tenant_info.encode())


class TestPlayerEvent:
    @pytest.mark.asyncio
    async def test_offline_event_schedules(self, client, mock_redis):
        _setup_auth(mock_redis)

        with patch(
            "src.core.scheduler.triggers.schedule_offline_analysis",
            AsyncMock(return_value="run-123"),
        ) as mock_schedule:
            response = await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "offline",
                    "timestamp": 1234567890.0,
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "scheduled"
        assert data["flow_run_id"] == "run-123"
        mock_schedule.assert_called_once_with(user_id="user-001", tenant_id="t-001", snapshot=None)

    @pytest.mark.asyncio
    async def test_offline_event_debounced(self, client, mock_redis):
        _setup_auth(mock_redis)

        with patch("src.core.scheduler.triggers.schedule_offline_analysis", AsyncMock(return_value=None)):
            response = await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "offline",
                    "timestamp": 1234567890.0,
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        assert response.json()["status"] == "debounced"

    @pytest.mark.asyncio
    async def test_online_event_cancels(self, client, mock_redis):
        _setup_auth(mock_redis)

        with patch("src.core.scheduler.triggers.cancel_offline_analysis", AsyncMock()) as mock_cancel:
            response = await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "online",
                    "timestamp": 1234567890.0,
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        mock_cancel.assert_called_once_with(user_id="user-001")

    @pytest.mark.asyncio
    async def test_unknown_event_type(self, client, mock_redis):
        _setup_auth(mock_redis)

        response = await client.post(
            "/webhooks/player-event",
            json={
                "user_id": "user-001",
                "event_type": "unknown",
                "timestamp": 1234567890.0,
            },
            headers=_auth_headers(),
        )

        assert response.status_code == 400
        assert "未知事件类型" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_fields_rejected(self, client, mock_redis):
        _setup_auth(mock_redis)

        response = await client.post(
            "/webhooks/player-event",
            json={"user_id": "user-001"},
            headers=_auth_headers(),
        )

        assert response.status_code == 422  # Validation error
