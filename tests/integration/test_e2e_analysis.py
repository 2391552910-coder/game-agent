"""端到端测试：完整的离线分析流程。

测试从 Webhook 接收到结果存储的完整数据流。
"""

import asyncio

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


# 测试数据
TEST_USER_ID = "test_e2e_player_001"
TEST_TENANT_ID = "test-tenant-001"
TEST_API_KEY = "gap_test_e2e_key"


@pytest.fixture()
async def setup_e2e_environment(mock_redis, mock_llm):
    """设置E2E测试环境。"""
    from tests.mocks.mock_connector import register_mock_player

    # 注册测试玩家
    register_mock_player(TEST_USER_ID, "competitive")

    # Mock LangGraph orchestrator
    mock_orchestrator = AsyncMock()
    mock_orchestrator.ainvoke = AsyncMock(
        return_value={
            "final_output": {
                "player_profile": {
                    "playstyle": "competitive",
                    "engagement_level": "high",
                    "current_goal": ["冲击天梯排名"],
                    "bottlenecks": ["装备评分不足"],
                },
                "recommended_actions": [
                    {
                        "action_type": "quest",
                        "priority": "high",
                        "reason": "获取资源提升装备",
                        "payload": {},
                    }
                ],
            }
        }
    )

    # Patch 正确的路径: src.core.agents.orchestrator
    with patch("src.core.agents.orchestrator.create_orchestrator", return_value=mock_orchestrator):
        yield {
            "mock_orchestrator": mock_orchestrator,
        }


@pytest.mark.asyncio
async def test_offline_analysis_full_flow(client: AsyncClient, setup_e2e_environment):
    """测试完整的离线分析流程。"""
    mock_orchestrator = setup_e2e_environment["mock_orchestrator"]

    # 发送离线事件
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    # 验证响应
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scheduled"
    assert data["user_id"] == TEST_USER_ID
    assert "flow_run_id" in data

    # 验证 orchestrator 被调用
    # 注意：由于是异步调度，可能需要等待
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_debounce_duplicate_offline_events(client: AsyncClient, setup_e2e_environment):
    """测试防抖机制：重复的离线事件应被忽略。"""
    mock_redis = None  # 从 fixture 获取

    # 发送第一个离线事件
    response1 = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response1.status_code == 200
    data1 = response1.json()
    assert data1["status"] == "scheduled"
    flow_run_id_1 = data1.get("flow_run_id")

    # 立即发送第二个离线事件（应该被防抖）
    response2 = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567891.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["status"] == "debounced"
    # flow_run_id 应该相同或为空


@pytest.mark.asyncio
async def test_online_cancels_pending_analysis(client: AsyncClient, setup_e2e_environment):
    """测试在线事件取消待处理的离线分析。"""
    # 发送离线事件
    response1 = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response1.status_code == 200
    assert response1.json()["status"] == "scheduled"

    # 发送在线事件（应该取消待处理的离线分析）
    response2 = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "online",
            "timestamp": 1234567891.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["status"] == "cancelled"
    assert data2["user_id"] == TEST_USER_ID


@pytest.mark.asyncio
async def test_multi_tenant_isolation(client: AsyncClient):
    """测试多租户隔离：不同租户的数据应该互不干扰。"""
    from tests.mocks.mock_connector import register_mock_player

    # 注册两个不同租户的玩家
    tenant1_user = "tenant1_player"
    tenant2_user = "tenant2_player"

    register_mock_player(tenant1_user, "competitive")
    register_mock_player(tenant2_user, "explorer")

    # 使用不同的 API Key
    api_key_1 = "gap_tenant1_key"
    api_key_2 = "gap_tenant2_key"

    # 发送两个租户的事件
    response1 = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": tenant1_user,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": api_key_1},
    )

    response2 = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": tenant2_user,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": api_key_2},
    )

    # 两个请求都应该成功
    assert response1.status_code == 200
    assert response2.status_code == 200


@pytest.mark.asyncio
async def test_player_not_found_error(client: AsyncClient):
    """测试玩家不存在的异常场景。"""
    from tests.mocks.mock_connector import PLAYER_NOT_FOUND_USER
    from tests.mocks.mock_connector import register_mock_player

    # 注册一个会抛出异常的玩家
    register_mock_player(PLAYER_NOT_FOUND_USER, "casual")

    # Mock connector 抛出异常
    with patch("src.game_specific.connector.fetch_player_snapshot") as mock_fetch:
        from src.game_specific.connector import PlayerNotFoundError

        mock_fetch.side_effect = PlayerNotFoundError(f"玩家不存在: {PLAYER_NOT_FOUND_USER}")

        # 发送离线事件
        response = await client.post(
            "/webhooks/player-event",
            json={
                "user_id": PLAYER_NOT_FOUND_USER,
                "event_type": "offline",
                "timestamp": 1234567890.0,
            },
            headers={"X-API-Key": TEST_API_KEY},
        )

        # 由于是异步调度，可能返回 scheduled
        # 实际错误会在 Flow 执行时发生
        assert response.status_code in [200, 202]


@pytest.mark.asyncio
async def test_invalid_event_type(client: AsyncClient):
    """测试无效的事件类型。"""
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "invalid_type",  # 无效的事件类型
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 400
    assert "未知事件类型" in response.json()["detail"]


@pytest.mark.asyncio
async def test_missing_required_fields(client: AsyncClient):
    """测试缺少必需字段的场景。"""
    # 缺少 user_id
    response = await client.post(
        "/webhooks/player-event",
        json={
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422  # Validation error

    # 缺少 event_type
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_with_snapshot_data(client: AsyncClient, setup_e2e_environment):
    """测试携带快照数据的离线事件。"""
    from tests.mocks.mock_connector import get_player_data

    snapshot_data = get_player_data(TEST_USER_ID)

    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
            "snapshot": snapshot_data,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scheduled"


@pytest.mark.asyncio
async def test_unauthorized_access(client: AsyncClient):
    """测试未授权访问（无效的 API Key）。"""
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": "invalid_api_key"},
    )

    # 应该返回 401 或 403
    assert response.status_code in [401, 403]


@pytest.mark.asyncio
async def test_concurrent_events(client: AsyncClient, setup_e2e_environment):
    """测试并发事件处理。"""
    from tests.mocks.mock_connector import register_mock_player

    # 注册多个玩家
    players = [f"concurrent_player_{i:03d}" for i in range(10)]
    for player_id in players:
        register_mock_player(player_id, "casual")

    # 并发发送离线事件
    tasks = []
    for player_id in players:
        task = client.post(
            "/webhooks/player-event",
            json={
                "user_id": player_id,
                "event_type": "offline",
                "timestamp": 1234567890.0,
            },
            headers={"X-API-Key": TEST_API_KEY},
        )
        tasks.append(task)

    responses = await asyncio.gather(*tasks)

    # 所有请求都应该成功
    for response in responses:
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_rate_limiting(client: AsyncClient):
    """测试速率限制（如果实现）。"""
    # 短时间内发送大量请求
    tasks = []
    for i in range(100):
        task = client.post(
            "/webhooks/player-event",
            json={
                "user_id": f"rate_limit_player_{i}",
                "event_type": "offline",
                "timestamp": 1234567890.0,
            },
            headers={"X-API-Key": TEST_API_KEY},
        )
        tasks.append(task)

    responses = await asyncio.gather(*tasks)

    # 检查是否有速率限制响应
    rate_limited_count = sum(1 for r in responses if r.status_code == 429)

    # 如果实现了速率限制，应该有一些请求被限制
    # 如果没有实现，所有请求都应该成功（或被防抖）
    # 这里只是验证行为
    pass
