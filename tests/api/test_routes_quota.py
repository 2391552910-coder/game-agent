"""配额查询路由测试。"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import make_quota_row


def _auth_headers():
    return {"X-API-Key": "valid-key"}


def _setup_auth(mock_redis):
    tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
    mock_redis.get = AsyncMock(return_value=tenant_info.encode())


class TestGetQuotaUsage:
    @pytest.mark.asyncio
    async def test_with_quota(self, client, mock_redis, mock_session):
        _setup_auth(mock_redis)
        session, ctx = mock_session

        quota_row = make_quota_row(monthly_limit=100000, used=5000)
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=quota_row)))

        response = await client.get(
            "/api/v1/quota/usage",
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["monthly_limit"] == 100000
        assert data["used"] == 5000
        assert data["remaining"] == 95000
        assert "5.0%" in data["usage_percent"]

    @pytest.mark.asyncio
    async def test_no_quota(self, client, mock_redis, mock_session):
        _setup_auth(mock_redis)
        session, ctx = mock_session

        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        response = await client.get(
            "/api/v1/quota/usage",
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        assert "未找到配额信息" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_full_usage(self, client, mock_redis, mock_session):
        _setup_auth(mock_redis)
        session, ctx = mock_session

        quota_row = make_quota_row(monthly_limit=100000, used=100000)
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=quota_row)))

        response = await client.get(
            "/api/v1/quota/usage",
            headers=_auth_headers(),
        )

        data = response.json()
        assert data["remaining"] == 0
        assert "100.0%" in data["usage_percent"]
