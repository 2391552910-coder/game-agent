"""Agent 工具单元测试。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.agents.tools import (
    _dynamic_rag_query,
    _query_player_history,
    _query_similar_players,
    create_tools,
)


class TestQueryPlayerHistory:
    @pytest.mark.asyncio
    async def test_with_records(self, mock_session):
        session, ctx = mock_session
        row = MagicMock()
        row.output_json = json.dumps({
            "player_profile": {"playstyle": "competitive", "engagement_level": "high", "current_goal": "level up", "bottlenecks": ["time"]},
            "recommended_actions": [{"action_type": "quest", "priority": "high"}],
        })
        row.analyzed_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        result = await _query_player_history("user-001", "tenant-001", limit=5)
        parsed = json.loads(result)

        assert len(parsed) == 1
        assert parsed[0]["playstyle"] == "competitive"

    @pytest.mark.asyncio
    async def test_no_records(self, mock_session):
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

        result = await _query_player_history("user-001", "tenant-001")
        assert "暂无历史分析记录" in result

    @pytest.mark.asyncio
    async def test_string_output_json(self, mock_session):
        """output_json 已经是字符串时的处理。"""
        session, ctx = mock_session
        output = json.dumps({"player_profile": {"playstyle": "explorer", "engagement_level": "medium", "current_goal": "", "bottlenecks": []}, "recommended_actions": []})
        row = MagicMock()
        row.output_json = output  # 字符串类型
        row.analyzed_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        result = await _query_player_history("user-001", "tenant-001")
        parsed = json.loads(result)
        assert parsed[0]["playstyle"] == "explorer"


class TestQuerySimilarPlayers:
    @pytest.mark.asyncio
    async def test_with_results(self, mock_session):
        session, ctx = mock_session
        row = MagicMock()
        row.user_id = "user-002"
        row.output_json = json.dumps({
            "player_profile": {"playstyle": "competitive", "engagement_level": "high", "current_goal": "rank", "bottlenecks": []},
            "recommended_actions": [{"action_type": "pvp", "priority": "medium", "target": "arena"}],
        })
        row.analyzed_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        result = await _query_similar_players("tenant-001", "user-001", playstyle="competitive")
        parsed = json.loads(result)

        assert len(parsed) == 1
        assert parsed[0]["user_id"] == "user-002"

    @pytest.mark.asyncio
    async def test_no_results(self, mock_session):
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

        result = await _query_similar_players("tenant-001", "user-001")
        assert "未找到相似玩家" in result

    @pytest.mark.asyncio
    async def test_without_playstyle_filter(self, mock_session):
        """不传 playstyle 时不添加 SQL 筛选条件。"""
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

        await _query_similar_players("tenant-001", "user-001", playstyle=None)

        # 验证 SQL 不包含 playstyle 条件
        call_args = session.execute.call_args
        sql_text = str(call_args[0][0])
        assert "playstyle" not in sql_text


class TestDynamicRagQuery:
    @pytest.mark.asyncio
    async def test_successful_query(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="PVP匹配规则: 随机匹配")

        with patch("src.core.engine.lightrag_engine.get_rag", AsyncMock(return_value=mock_rag)):
            result = await _dynamic_rag_query("PVP匹配机制")
            assert "PVP" in result

    @pytest.mark.asyncio
    async def test_empty_result(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="")

        with patch("src.core.engine.lightrag_engine.get_rag", AsyncMock(return_value=mock_rag)):
            result = await _dynamic_rag_query("不存在的主题")
            assert "未找到" in result


class TestCreateTools:
    def test_returns_three_tools(self):
        tools = create_tools("tenant-001", "user-001")
        assert len(tools) == 3
        names = {t.name for t in tools}
        assert names == {"query_player_history", "query_similar_players", "dynamic_rag_query"}

    def test_tools_have_descriptions(self):
        tools = create_tools("tenant-001", "user-001")
        for t in tools:
            assert t.description, f"工具 {t.name} 缺少描述"
