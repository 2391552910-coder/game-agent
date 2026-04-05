"""租户注册路由测试。"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def _auth_headers():
    return {"X-API-Key": "valid-key"}


def _setup_auth(mock_redis):
    tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
    mock_redis.get = AsyncMock(return_value=tenant_info.encode())


class TestRegisterTenant:
    @pytest.mark.asyncio
    async def test_successful_registration(self, client, mock_redis, mock_session):
        _setup_auth(mock_redis)
        session, ctx = mock_session

        # 路由有 4 次 session.execute:
        # 1. SELECT 检查 user_id (first() → None)
        # 2. INSERT tenant (不需要返回值)
        # 3. SELECT 获取 tenant_id (scalar() → uuid)
        # 4. INSERT quota (不需要返回值)
        check_result = MagicMock()
        check_result.first = MagicMock(return_value=None)

        tenant_id_result = MagicMock()
        tenant_id_result.scalar = MagicMock(return_value="new-tenant-uuid")

        session.execute = AsyncMock(side_effect=[
            check_result,       # 1. 检查 user_id 是否已注册
            MagicMock(),        # 2. INSERT tenant
            tenant_id_result,   # 3. 获取 tenant_id
            MagicMock(),        # 4. 创建配额
        ])

        response = await client.post(
            "/api/v1/tenants/register",
            json={"user_id": "new-user"},
            headers=_auth_headers(),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "new-user"
        assert data["api_key"].startswith("gap_")
        assert data["tenant_id"] == "new-tenant-uuid"

    @pytest.mark.asyncio
    async def test_duplicate_user_rejected(self, client, mock_redis, mock_session):
        _setup_auth(mock_redis)
        session, ctx = mock_session

        existing_row = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=existing_row)))

        response = await client.post(
            "/api/v1/tenants/register",
            json={"user_id": "existing-user"},
            headers=_auth_headers(),
        )

        assert response.status_code == 409
        assert "已注册" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_user_id(self, client, mock_redis):
        _setup_auth(mock_redis)

        response = await client.post(
            "/api/v1/tenants/register",
            json={},
            headers=_auth_headers(),
        )

        assert response.status_code == 422
