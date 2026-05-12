"""behavior_checkpoint Webhook 端点测试。"""

import json
from unittest.mock import AsyncMock, patch

import pytest


def _auth_headers():
    return {"X-API-Key": "valid-key"}


def _setup_auth(mock_redis):
    tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
    mock_redis.get = AsyncMock(return_value=tenant_info.encode())


class TestBehaviorCheckpoint:
    @pytest.mark.asyncio
    async def test_behavior_checkpoint_recorded(self, client, mock_redis):
        """behavior_checkpoint 事件正常写入，返回 recorded"""
        _setup_auth(mock_redis)

        with patch(
            "src.api.routes.webhooks._write_behavior_event",
            new_callable=AsyncMock,
        ) as mock_write:
            response = await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "behavior_checkpoint",
                    "timestamp": 1700000000.0,
                    "session_id": "session-abc",
                    "behavior_event": {
                        "type": "move",
                        "data": {"from": "广场", "to": "咖啡馆"},
                    },
                    "snapshot": {"level": 10, "gold": 500},
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "recorded"
        assert data["user_id"] == "user-001"
        mock_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_behavior_checkpoint_write_args(self, client, mock_redis):
        """_write_behavior_event 被调用时参数正确"""
        _setup_auth(mock_redis)

        with patch(
            "src.api.routes.webhooks._write_behavior_event",
            new_callable=AsyncMock,
        ) as mock_write:
            await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "behavior_checkpoint",
                    "timestamp": 1700000000.0,
                    "session_id": "session-abc",
                    "behavior_event": {"type": "purchase", "data": {"item": "sword"}},
                    "snapshot": {"gold": 300},
                },
                headers=_auth_headers(),
            )

        call_kwargs = mock_write.call_args.kwargs
        assert call_kwargs["user_id"] == "user-001"
        assert call_kwargs["session_id"] == "session-abc"
        assert call_kwargs["behavior_event"]["type"] == "purchase"
        assert call_kwargs["snapshot"] == {"gold": 300}

    @pytest.mark.asyncio
    async def test_behavior_checkpoint_missing_session_id(self, client, mock_redis):
        """behavior_checkpoint 缺少 session_id 时返回 422"""
        _setup_auth(mock_redis)

        response = await client.post(
            "/webhooks/player-event",
            json={
                "user_id": "user-001",
                "event_type": "behavior_checkpoint",
                "timestamp": 1700000000.0,
                # 缺少 session_id
                "behavior_event": {"type": "move", "data": {}},
            },
            headers=_auth_headers(),
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_behavior_checkpoint_no_snapshot(self, client, mock_redis):
        """behavior_checkpoint snapshot 可选，不传时正常处理"""
        _setup_auth(mock_redis)

        with patch(
            "src.api.routes.webhooks._write_behavior_event",
            new_callable=AsyncMock,
        ) as mock_write:
            response = await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "behavior_checkpoint",
                    "timestamp": 1700000000.0,
                    "session_id": "session-abc",
                    "behavior_event": {"type": "interact", "data": {}},
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        call_kwargs = mock_write.call_args.kwargs
        assert call_kwargs["snapshot"] is None

    @pytest.mark.asyncio
    async def test_unknown_event_type_still_400(self, client, mock_redis):
        """未知事件类型仍返回 400，不受新增类型影响"""
        _setup_auth(mock_redis)

        response = await client.post(
            "/webhooks/player-event",
            json={
                "user_id": "user-001",
                "event_type": "unknown_type",
                "timestamp": 1700000000.0,
            },
            headers=_auth_headers(),
        )

        assert response.status_code == 400
        assert "未知事件类型" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_offline_event_unaffected(self, client, mock_redis):
        """新增 behavior_checkpoint 不影响原有 offline 事件"""
        _setup_auth(mock_redis)

        with patch(
            "src.core.scheduler.triggers.schedule_offline_analysis",
            AsyncMock(return_value="run-456"),
        ):
            response = await client.post(
                "/webhooks/player-event",
                json={
                    "user_id": "user-001",
                    "event_type": "offline",
                    "timestamp": 1700000000.0,
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        assert response.json()["status"] == "scheduled"
