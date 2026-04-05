"""分析结果查询路由测试。"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import make_analysis_row


class TestGetLatestAnalysis:
    @pytest.mark.asyncio
    async def test_found(self, client, mock_redis, mock_session):
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())

        row = make_analysis_row()
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=row)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "valid-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "user-001"
        assert "output" in data
        assert data["output"]["player_profile"]["playstyle"] == "competitive"

    @pytest.mark.asyncio
    async def test_not_found(self, client, mock_redis, mock_session):
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())

        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        response = await client.get(
            "/api/v1/analysis/user-001/latest",
            headers={"X-API-Key": "valid-key"},
        )

        assert response.status_code == 404


class TestGetAnalysisHistory:
    @pytest.mark.asyncio
    async def test_with_results(self, client, mock_redis, mock_session):
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())

        rows = [make_analysis_row(), make_analysis_row()]
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=rows)))

        response = await client.get(
            "/api/v1/analysis/user-001/history",
            headers={"X-API-Key": "valid-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["history"]) == 2

    @pytest.mark.asyncio
    async def test_empty_history(self, client, mock_redis, mock_session):
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())

        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

        response = await client.get(
            "/api/v1/analysis/user-001/history",
            headers={"X-API-Key": "valid-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_limit_parameter(self, client, mock_redis, mock_session):
        tenant_info = json.dumps({"tenant_id": "t-001", "is_admin": False})
        mock_redis.get = AsyncMock(return_value=tenant_info.encode())

        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

        response = await client.get(
            "/api/v1/analysis/user-001/history?limit=5",
            headers={"X-API-Key": "valid-key"},
        )

        assert response.status_code == 200
        # 验证 limit 参数传入了 SQL 查询
        call_args = session.execute.call_args
        params = call_args[0][1]
        assert params["limit"] == 5
