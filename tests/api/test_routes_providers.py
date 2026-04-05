"""LLM Provider 管理 API 测试。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_provider_row


def _admin_headers():
    return {"X-API-Key": "admin-test-key"}


def _normal_headers():
    return {"X-API-Key": "normal-key"}


def _setup_admin_auth(mock_redis):
    tenant_info = json.dumps({"tenant_id": "admin-001", "is_admin": True})
    mock_redis.get = AsyncMock(return_value=tenant_info.encode())


def _setup_normal_auth(mock_redis):
    tenant_info = json.dumps({"tenant_id": "normal-001", "is_admin": False})
    mock_redis.get = AsyncMock(return_value=tenant_info.encode())


class TestListProviders:
    @pytest.mark.asyncio
    async def test_admin_can_list(self, client, mock_redis, mock_session):
        _setup_admin_auth(mock_redis)
        session, ctx = mock_session

        rows = [make_provider_row(name="P1"), make_provider_row(name="P2")]
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=rows)))

        response = await client.get(
            "/api/v1/providers",
            headers=_admin_headers(),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self, client, mock_redis):
        _setup_normal_auth(mock_redis)

        response = await client.get(
            "/api/v1/providers",
            headers=_normal_headers(),
        )

        assert response.status_code == 403


class TestCreateProvider:
    @pytest.mark.asyncio
    async def test_admin_can_create(self, client, mock_redis, mock_session):
        _setup_admin_auth(mock_redis)
        session, ctx = mock_session

        new_row = make_provider_row(name="NewProvider", model="gpt-4")
        session.execute = AsyncMock(return_value=MagicMock(fetchone=MagicMock(return_value=new_row)))

        with patch("src.api.routes.providers._invalidate_balancer_cache") as mock_inv:
            response = await client.post(
                "/api/v1/providers",
                json={
                    "name": "NewProvider",
                    "provider": "openai",
                    "model": "gpt-4",
                    "api_key": "sk-xxx",
                    "base_url": "https://api.openai.com/v1",
                    "weight": 2,
                },
                headers=_admin_headers(),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "NewProvider"
        # api_key 不在响应中
        assert "api_key" not in data

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self, client, mock_redis):
        _setup_normal_auth(mock_redis)

        response = await client.post(
            "/api/v1/providers",
            json={
                "name": "Test",
                "provider": "test",
                "model": "test",
                "api_key": "sk-x",
                "base_url": "http://test",
            },
            headers=_normal_headers(),
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_payload(self, client, mock_redis):
        _setup_admin_auth(mock_redis)

        response = await client.post(
            "/api/v1/providers",
            json={"name": ""},  # 缺少必填字段
            headers=_admin_headers(),
        )

        assert response.status_code == 422


class TestUpdateProvider:
    @pytest.mark.asyncio
    async def test_admin_can_update(self, client, mock_redis, mock_session):
        _setup_admin_auth(mock_redis)
        session, ctx = mock_session

        updated_row = make_provider_row(name="Updated", weight=5)
        session.execute = AsyncMock(return_value=MagicMock(fetchone=MagicMock(return_value=updated_row)))

        with patch("src.api.routes.providers._invalidate_balancer_cache"):
            response = await client.put(
                "/api/v1/providers/some-uuid",
                json={"weight": 5, "name": "Updated"},
                headers=_admin_headers(),
            )

        assert response.status_code == 200
        assert response.json()["weight"] == 5

    @pytest.mark.asyncio
    async def test_not_found(self, client, mock_redis, mock_session):
        _setup_admin_auth(mock_redis)
        session, ctx = mock_session

        session.execute = AsyncMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))

        response = await client.put(
            "/api/v1/providers/nonexistent",
            json={"weight": 3},
            headers=_admin_headers(),
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_update_rejected(self, client, mock_redis):
        _setup_admin_auth(mock_redis)

        response = await client.put(
            "/api/v1/providers/some-uuid",
            json={},
            headers=_admin_headers(),
        )

        assert response.status_code == 400


class TestDeleteProvider:
    @pytest.mark.asyncio
    async def test_admin_can_delete(self, client, mock_redis, mock_session):
        _setup_admin_auth(mock_redis)
        session, ctx = mock_session

        mock_result = MagicMock()
        mock_result.rowcount = 1
        session.execute = AsyncMock(return_value=mock_result)

        with patch("src.api.routes.providers._invalidate_balancer_cache"):
            response = await client.delete(
                "/api/v1/providers/some-uuid",
                headers=_admin_headers(),
            )

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_not_found(self, client, mock_redis, mock_session):
        _setup_admin_auth(mock_redis)
        session, ctx = mock_session

        mock_result = MagicMock()
        mock_result.rowcount = 0
        session.execute = AsyncMock(return_value=mock_result)

        response = await client.delete(
            "/api/v1/providers/nonexistent",
            headers=_admin_headers(),
        )

        assert response.status_code == 404
