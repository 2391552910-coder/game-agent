"""API 中间件测试。"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_tenant_row


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_public_paths_no_auth(self, client, mock_redis):
        """公开路径不需要 API Key。"""
        response = await client.get("/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_api_key(self, client, mock_redis):
        """缺少 X-API-Key 返回 401。"""
        response = await client.get("/api/v1/analysis/user-001/latest")
        assert response.status_code == 401
        assert "缺少" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_api_key(self, client, mock_redis, mock_session):
        """无效 API Key 返回 401。"""
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "invalid-key"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key_via_redis_cache(self, client, mock_redis, mock_session):
        """Redis 缓存命中的有效认证。"""
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())

        # 让后续路由处理正常工作（可能返回 404 等，但认证通过）
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "valid-key"},
        )
        # 认证通过（404 是因为查询结果为空，不是 401）
        assert response.status_code != 401

    @pytest.mark.asyncio
    async def test_valid_api_key_via_db(self, client, mock_redis, mock_session):
        """Redis 未命中时从 DB 验证。"""
        mock_redis.get = AsyncMock(return_value=None)

        tenant_row = make_tenant_row(tenant_id="t-001")
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=tenant_row)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "valid-key"},
        )
        # 认证通过（不返回 401）
        assert response.status_code != 401

        # 验证 Redis 缓存被写入
        mock_redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_inactive_tenant_rejected(self, client, mock_redis, mock_session):
        """不活跃的 tenant 被拒绝。"""
        mock_redis.get = AsyncMock(return_value=None)

        tenant_row = make_tenant_row(is_active=False)
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=tenant_row)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "inactive-key"},
        )
        assert response.status_code == 401


class TestRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_under_limit_passes(self, client, mock_redis, mock_session):
        """请求量未超限正常通过。"""
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())
        # pipeline execute 返回 [removed=0, added=1, count=1, expire=True]
        mock_redis.execute = AsyncMock(return_value=[0, 1, 1, True])

        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "valid-key"},
        )
        assert response.status_code != 429
