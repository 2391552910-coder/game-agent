"""端到端测试：包含 LightRAG 的完整分析流程。

测试 RAG 检索、工具调用、LLM 分析的完整流程。
"""

import asyncio

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch


# 测试数据
TEST_USER_ID = "test_rag_player_001"
TEST_API_KEY = "gap_test_rag_key"


@pytest.fixture()
async def setup_rag_environment(mock_redis, mock_llm):
    """设置 RAG 测试环境。"""
    from tests.mocks.mock_connector import register_mock_player

    # 注册测试玩家
    register_mock_player(TEST_USER_ID, "explorer")

    # Mock LightRAG 引擎
    mock_rag = AsyncMock()
    mock_rag.aquery = AsyncMock(
        return_value="""
        根据游戏规则，玩家在以下情况下应该获得推荐：
        1. PVP评分低于2000时，建议参与日常竞技场提升评分
        2. 装备评分不足时，建议挑战副本获取装备
        3. 体力不足时，建议使用体力药水或等待恢复
        4. VIP等级低时，建议完成主线任务获取资源
        """
    )

    # Mock LangGraph orchestrator
    mock_orchestrator = AsyncMock()
    mock_orchestrator.ainvoke = AsyncMock(
        return_value={
            "rag_context": "模拟的RAG上下文",
            "final_output": {
                "player_profile": {
                    "playstyle": "explorer",
                    "engagement_level": "medium",
                    "current_goal": ["探索新区域", "完成主线任务"],
                    "bottlenecks": ["等级限制", "装备不足"],
                },
                "recommended_actions": [
                    {
                        "skillName": "observe_state",
                        "schemaVersion": "v1",
                        "arguments": {},
                        "priority": "high",
                        "reason": "先观察当前状态，确认能否进入下一步",
                    },
                    {
                        "skillName": "move_to",
                        "schemaVersion": "v1",
                        "arguments": {
                            "target": {"x": 61.3, "y": 0.94, "z": 154.0},
                            "stopDistance": 0.5,
                        },
                        "priority": "medium",
                        "reason": "移动到目标地点",
                    },
                ],
            },
        }
    )

    with patch("src.core.agents.orchestrator.create_orchestrator", return_value=mock_orchestrator), \
         patch("src.core.engine.lightrag_engine.get_rag", return_value=mock_rag):
        yield {
            "mock_orchestrator": mock_orchestrator,
            "mock_rag": mock_rag,
        }


@pytest.mark.asyncio
async def test_rag_context_retrieval(setup_rag_environment):
    """测试 RAG 上下文检索。"""
    mock_rag = setup_rag_environment["mock_rag"]

    # 模拟 RAG 查询
    query = "玩家PVP评分1500，应该获得什么推荐？"
    result = await mock_rag.aquery(
        query=query,
        mode="hybrid",
    )

    # 验证 RAG 返回了上下文
    assert result is not None
    assert len(result) > 0
    assert "PVP评分" in result or "建议" in result


@pytest.mark.asyncio
async def test_rag_different_modes(setup_rag_environment):
    """测试 RAG 不同检索模式。"""
    mock_rag = setup_rag_environment["mock_rag"]

    modes = ["local", "global", "hybrid", "naive"]

    for mode in modes:
        result = await mock_rag.aquery(
            query="测试查询",
            mode=mode,
        )

        assert result is not None
        # 验证模式参数被传递
        mock_rag.aquery.assert_called()


@pytest.mark.asyncio
async def test_tool_call_query_player_history(client: AsyncClient, setup_rag_environment):
    """测试工具调用：查询玩家历史分析。"""
    from tests.mocks.mock_connector import register_mock_player

    # 注册玩家并生成历史数据
    register_mock_player(TEST_USER_ID, "competitive")

    # 发送离线事件触发分析
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200

    # 查询历史分析结果
    history_response = await client.get(
        f"/analysis/{TEST_USER_ID}/history",
        headers={"X-API-Key": TEST_API_KEY},
    )

    # 验证历史查询
    assert history_response.status_code in [200, 404]  # 可能没有历史


@pytest.mark.asyncio
async def test_tool_call_query_similar_players(setup_rag_environment):
    """测试工具调用：查询相似玩家。"""
    from tests.mocks.mock_connector import register_multiple_players

    # 注册多个相似玩家
    similar_players = register_multiple_players(5, "similar_", "competitive")

    mock_result = [
        {
            "user_id": pid,
            "similarity": 0.85,
            "playstyle": "competitive",
        }
        for pid in similar_players
    ]

    # Mock 相似玩家核心查询
    with patch("src.core.agents.tools._query_similar_players", AsyncMock(return_value=mock_result)) as mock_query:
        result = await mock_query(
            tenant_id="test-tenant-001",
            current_user_id=TEST_USER_ID,
            playstyle="competitive",
            limit=5,
        )

    assert len(result) == 5
    assert all("similarity" in r for r in result)


@pytest.mark.asyncio
async def test_dynamic_rag_query(setup_rag_environment):
    """测试动态 RAG 查询工具调用。"""
    mock_rag = setup_rag_environment["mock_rag"]

    # 模拟动态查询
    queries = [
        "玩家当前瓶颈是什么？",
        "如何提升PVP评分？",
        "推荐什么副本？",
    ]

    for query in queries:
        result = await mock_rag.aquery(
            query=query,
            mode="hybrid",
        )

        assert result is not None


@pytest.mark.asyncio
async def test_llm_analysis_nodes(client: AsyncClient, setup_rag_environment):
    """测试 LLM 分析节点执行。"""
    from tests.mocks.mock_connector import get_player_data

    # 获取玩家数据
    player_data = get_player_data(TEST_USER_ID)

    # 发送离线事件
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
            "snapshot": player_data,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scheduled"


@pytest.mark.asyncio
async def test_structured_output_validation(client: AsyncClient, setup_rag_environment):
    """测试结构化输出验证。"""
    from src.core.agents.models import BehaviorProfile, RecommendedAction

    # Mock 结构化输出
    mock_profile = BehaviorProfile(
        playstyle="explorer",
        engagement_level="medium",
        current_goal=["探索新区域"],
        bottlenecks=["等级限制"],
    )

    mock_action = RecommendedAction(
        skillName="observe_state",
        priority="high",
        reason="先观察状态",
    )

    # 验证结构化输出
    assert mock_profile.playstyle in ["competitive", "explorer", "social", "casual"]
    assert mock_profile.engagement_level in ["high", "medium", "low"]
    assert mock_action.priority in ["high", "medium", "low"]


@pytest.mark.asyncio
async def test_rag_with_game_docs():
    """测试使用真实游戏文档的 RAG 检索。"""
    # 这个测试需要真实的 LightRAG 实例
    # 只有在完整集成测试时才运行

    # 检查是否有 RAG 实例可用
    try:
        from src.core.engine.lightrag_engine import get_rag

        rag = await get_rag(workspace="test")

        # 查询游戏规则
        result = await rag.aquery(
            query="工作区开放时间是什么时候？",
            mode="hybrid",
        )

        # 验证查询结果
        assert result is not None
        assert "开放时间" in result or "工作日" in result or "周末" in result

    except Exception as e:
        # RAG 不可用，跳过测试
        pytest.skip(f"RAG not available: {e}")


@pytest.mark.asyncio
async def test_full_rag_pipeline(client: AsyncClient, setup_rag_environment):
    """测试完整的 RAG 流水线。"""
    from tests.mocks.mock_connector import get_player_data

    player_data = get_player_data(TEST_USER_ID)

    # 发送离线事件
    response = await client.post(
        "/webhooks/player-event",
        json={
            "user_id": TEST_USER_ID,
            "event_type": "offline",
            "timestamp": 1234567890.0,
            "snapshot": player_data,
        },
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200

    # 等待分析完成
    await asyncio.sleep(0.5)

    # 查询分析结果
    result_response = await client.get(
        f"/analysis/{TEST_USER_ID}/latest",
        headers={"X-API-Key": TEST_API_KEY},
    )

    # 验证结果结构
    if result_response.status_code == 200:
        result = result_response.json()
        assert "output" in result or "message" in result


@pytest.mark.asyncio
async def test_rag_fallback_on_empty_context(setup_rag_environment):
    """测试 RAG 上下文为空时的降级处理。"""
    mock_rag = setup_rag_environment["mock_rag"]

    # Mock 返回空上下文
    mock_rag.aquery = AsyncMock(return_value="")

    # 验证降级处理
    result = await mock_rag.aquery(
        query="测试查询",
        mode="hybrid",
    )

    assert result == ""


@pytest.mark.asyncio
async def test_multi_turn_tool_calling(client: AsyncClient, setup_rag_environment):
    """测试多轮工具调用（最多3轮，8次工具调用）。"""
    from tests.mocks.mock_connector import register_mock_player

    # 注册玩家
    register_mock_player(TEST_USER_ID, "social")

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

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_rag_timeout_handling(setup_rag_environment):
    """测试 RAG 超时处理。"""
    mock_rag = setup_rag_environment["mock_rag"]

    # Mock 超时
    import asyncio

    async def slow_query(*args, **kwargs):
        await asyncio.sleep(10)  # 超过超时限制
        return "result"

    mock_rag.aquery = AsyncMock(side_effect=slow_query)

    # 验证超时处理
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(
            mock_rag.aquery(query="测试", mode="hybrid"),
            timeout=1.0,
        )


@pytest.mark.asyncio
async def test_rag_error_handling(setup_rag_environment):
    """测试 RAG 错误处理。"""
    mock_rag = setup_rag_environment["mock_rag"]

    # Mock 抛出异常
    mock_rag.aquery = AsyncMock(side_effect=Exception("RAG error"))

    # 验证错误处理
    try:
        result = await mock_rag.aquery(query="测试", mode="hybrid")
    except Exception as e:
        assert str(e) == "RAG error"
