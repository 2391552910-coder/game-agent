"""Agent 工具单元测试。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.agents.tools import (
    _detect_anomaly,
    _dynamic_rag_query,
    _extract_metric,
    _get_action_tracking,
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
    def test_returns_five_tools(self):
        tools = create_tools("tenant-001", "user-001")
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert names == {
            "query_player_history",
            "query_similar_players",
            "dynamic_rag_query",
            "get_action_tracking",
            "detect_anomaly",
        }

    def test_tools_have_descriptions(self):
        tools = create_tools("tenant-001", "user-001")
        for t in tools:
            assert t.description, f"工具 {t.name} 缺少描述"

    def test_snapshot_optional(self):
        """不传 snapshot 时工具正常创建。"""
        tools = create_tools("tenant-001", "user-001")
        assert len(tools) == 5

    def test_snapshot_injected(self):
        """传入 snapshot 时工具正常创建。"""
        snapshot = {"learning_courses": 8, "stats": {"play_hours": 100}}
        tools = create_tools("tenant-001", "user-001", snapshot=snapshot)
        assert len(tools) == 5


class TestExtractMetric:
    def test_top_level_field(self):
        snapshot = {"learning_courses": 11, "other": "value"}
        assert _extract_metric(snapshot, "learning_courses") == 11.0

    def test_stats_nested_field(self):
        snapshot = {"stats": {"play_hours": 180.5}}
        assert _extract_metric(snapshot, "play_hours") == 180.5

    def test_top_level_takes_priority(self):
        """顶层字段优先于 stats 嵌套字段。"""
        snapshot = {"play_hours": 10, "stats": {"play_hours": 999}}
        assert _extract_metric(snapshot, "play_hours") == 10.0

    def test_missing_field_returns_none(self):
        snapshot = {"other": 5}
        assert _extract_metric(snapshot, "nonexistent") is None

    def test_non_numeric_returns_none(self):
        snapshot = {"level": "gold"}
        assert _extract_metric(snapshot, "level") is None

    def test_integer_value(self):
        snapshot = {"score": 42}
        assert _extract_metric(snapshot, "score") == 42.0

    def test_empty_snapshot(self):
        assert _extract_metric({}, "any_metric") is None


class TestGetActionTracking:
    @pytest.mark.asyncio
    async def test_no_tracking_records(self, mock_session):
        session, ctx = mock_session
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))

        result = await _get_action_tracking("user-001", "tenant-001", {})
        assert "首次分析" in result

    @pytest.mark.asyncio
    async def test_completed_by_metric(self, mock_session):
        """指标达标时状态应为 completed。"""
        session, ctx = mock_session
        row = MagicMock()
        row.action_type = "complete_course"
        row.action_desc = "完成3个课程"
        row.goal_metric = "learning_courses"
        row.goal_value = 11.0
        row.baseline_value = 8.0
        row.deadline = None
        row.created_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        snapshot = {"learning_courses": 11}
        result = await _get_action_tracking("user-001", "tenant-001", snapshot)

        assert "completed" in result
        assert "100%" in result

    @pytest.mark.asyncio
    async def test_timeout_by_deadline(self, mock_session):
        """超过截止时间且指标未达标时状态应为 timeout。"""
        from datetime import timedelta, timezone
        session, ctx = mock_session
        row = MagicMock()
        row.action_type = "complete_course"
        row.action_desc = "完成课程"
        row.goal_metric = "learning_courses"
        row.goal_value = 11.0
        row.baseline_value = 8.0
        row.deadline = datetime.now(timezone.utc) - timedelta(hours=1)
        row.created_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        snapshot = {"learning_courses": 9}  # 未达标
        result = await _get_action_tracking("user-001", "tenant-001", snapshot)

        assert "timeout" in result

    @pytest.mark.asyncio
    async def test_in_progress(self, mock_session):
        """指标未达标且未超时时状态应为 tracking。"""
        from datetime import timedelta, timezone
        session, ctx = mock_session
        row = MagicMock()
        row.action_type = "complete_course"
        row.action_desc = "完成课程"
        row.goal_metric = "learning_courses"
        row.goal_value = 11.0
        row.baseline_value = 8.0
        row.deadline = datetime.now(timezone.utc) + timedelta(hours=48)
        row.created_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        snapshot = {"learning_courses": 9}
        result = await _get_action_tracking("user-001", "tenant-001", snapshot)

        assert "tracking" in result
        assert "进行中 1" in result

    @pytest.mark.asyncio
    async def test_missing_metric_in_snapshot(self, mock_session):
        """快照中找不到 goal_metric 时应有提示。"""
        session, ctx = mock_session
        row = MagicMock()
        row.action_type = "complete_course"
        row.action_desc = "完成课程"
        row.goal_metric = "learning_courses"
        row.goal_value = 11.0
        row.baseline_value = 8.0
        row.deadline = None
        row.created_at = datetime.now(UTC)
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[row])))

        snapshot = {}  # 快照中没有 learning_courses
        result = await _get_action_tracking("user-001", "tenant-001", snapshot)

        assert "未找到该指标" in result


class TestDetectAnomaly:
    def _make_timeout_result(self, count: int):
        row = MagicMock()
        row.cnt = count
        return MagicMock(first=MagicMock(return_value=row))

    def _make_history_result(self, bottlenecks: list):
        row = MagicMock()
        row.output_json = json.dumps({
            "player_profile": {
                "playstyle": "competitive",
                "engagement_level": "high",
                "current_goal": [],
                "bottlenecks": bottlenecks,
            },
            "recommended_actions": [],
        })
        return MagicMock(first=MagicMock(return_value=row))

    @pytest.mark.asyncio
    async def test_no_anomaly(self, mock_session):
        session, ctx = mock_session
        execute_results = [
            self._make_timeout_result(0),
            self._make_history_result(["资金不足"]),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        snapshot = {"bottlenecks": ["时间不足"]}  # 与历史不同
        result = await _detect_anomaly("user-001", "tenant-001", snapshot)
        assert result == "无异常"

    @pytest.mark.asyncio
    async def test_action_timeout(self, mock_session):
        session, ctx = mock_session
        execute_results = [
            self._make_timeout_result(2),
            self._make_history_result([]),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        result = await _detect_anomaly("user-001", "tenant-001", {})
        assert "行动超时" in result
        assert "2 条" in result

    @pytest.mark.asyncio
    async def test_repeated_bottleneck(self, mock_session):
        session, ctx = mock_session
        bottlenecks = ["资金不足", "缺乏社交互动"]
        execute_results = [
            self._make_timeout_result(0),
            self._make_history_result(bottlenecks),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        snapshot = {"bottlenecks": ["缺乏社交互动", "资金不足"]}  # 顺序不同但内容相同
        result = await _detect_anomaly("user-001", "tenant-001", snapshot)
        assert "重复卡关" in result

    @pytest.mark.asyncio
    async def test_both_anomalies(self, mock_session):
        session, ctx = mock_session
        bottlenecks = ["资金不足"]
        execute_results = [
            self._make_timeout_result(1),
            self._make_history_result(bottlenecks),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        snapshot = {"bottlenecks": ["资金不足"]}
        result = await _detect_anomaly("user-001", "tenant-001", snapshot)
        assert "行动超时" in result
        assert "重复卡关" in result

    @pytest.mark.asyncio
    async def test_no_history(self, mock_session):
        """无历史记录时只检测行动超时。"""
        session, ctx = mock_session
        execute_results = [
            self._make_timeout_result(0),
            MagicMock(first=MagicMock(return_value=None)),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        result = await _detect_anomaly("user-001", "tenant-001", {})
        assert result == "无异常"

    @pytest.mark.asyncio
    async def test_empty_bottlenecks_no_anomaly(self, mock_session):
        """历史或当前 bottlenecks 为空时不触发重复卡关。"""
        session, ctx = mock_session
        execute_results = [
            self._make_timeout_result(0),
            self._make_history_result([]),  # 历史为空
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        snapshot = {"bottlenecks": []}
        result = await _detect_anomaly("user-001", "tenant-001", snapshot)
        assert result == "无异常"
