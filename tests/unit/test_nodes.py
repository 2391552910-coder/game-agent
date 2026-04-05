"""LangGraph 节点单元测试。

所有外部依赖（LLM, RAG, DB）均被 mock，只测试节点逻辑。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.agents.models import ActionList, BehaviorProfile, RecommendedAction
from src.core.agents.nodes import (
    _build_rag_query,
    action_reasoning_node,
    behavior_analysis_node,
    fetch_snapshot_node,
    gather_context_node,
    merge_output_node,
    retrieve_rag_context_node,
)


def _base_state(**overrides) -> dict:
    """创建基础测试 state。"""
    state = {
        "user_id": "user-001",
        "tenant_id": "tenant-001",
        "snapshot": {
            "user_id": "user-001",
            "player_name": "TestPlayer",
            "level": 25,
            "guild": "测试公会",
            "stats": {"play_hours": 120, "quests_completed": 45},
        },
        "rag_context": "",
        "enriched_context": "",
        "behavior_report": "",
        "reasoned_actions": [],
        "final_output": {},
        "errors": [],
    }
    state.update(overrides)
    return state


# ── fetch_snapshot_node ──


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_valid_snapshot(self):
        state = _base_state()
        result = await fetch_snapshot_node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_missing_snapshot(self):
        state = _base_state(snapshot=None)
        result = await fetch_snapshot_node(state)
        assert "errors" in result
        assert "snapshot为空" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_empty_snapshot_is_falsy(self):
        """空 dict {} 在 Python 中是 falsy，节点会视为无效。"""
        state = _base_state(snapshot={})
        result = await fetch_snapshot_node(state)
        assert "errors" in result


# ── retrieve_rag_context_node ──


class TestRetrieveRagContext:
    @pytest.mark.asyncio
    async def test_successful_retrieval(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="商业区开放时间: 9:00-22:00")

        with patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)):
            state = _base_state()
            result = await retrieve_rag_context_node(state)

        assert result["rag_context"] == "商业区开放时间: 9:00-22:00"

    @pytest.mark.asyncio
    async def test_rag_failure_returns_error(self):
        with patch("src.core.agents.nodes.get_rag", AsyncMock(side_effect=Exception("RAG down"))):
            state = _base_state()
            result = await retrieve_rag_context_node(state)

        assert result["rag_context"] == ""
        assert any("RAG 检索失败" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_empty_snapshot(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="")

        with patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)):
            result = await retrieve_rag_context_node(_base_state(snapshot={}))

        assert result["rag_context"] == ""


# ── _build_rag_query ──


class TestBuildRagQuery:
    def test_extracts_text_values(self):
        snapshot = {"current_area": "商业区", "profession": "程序员", "level": 25}
        query = _build_rag_query(snapshot)
        assert "商业区" in query
        assert "程序员" in query
        assert "25" not in query

    def test_excludes_id_fields(self):
        snapshot = {"user_id": "u-001", "guild_id": "g-001", "name": "测试"}
        query = _build_rag_query(snapshot)
        assert "u-001" not in query
        assert "测试" in query

    def test_handles_nested_dict(self):
        snapshot = {"meta": {"location": "北京", "type": "VIP"}}
        query = _build_rag_query(snapshot)
        assert "北京" in query
        assert "VIP" in query

    def test_handles_list_values(self):
        snapshot = {"tags": ["PVP", "竞技", "高手"]}
        query = _build_rag_query(snapshot)
        assert "PVP" in query
        assert "竞技" in query

    def test_empty_snapshot(self):
        assert _build_rag_query({}) == ""
        assert _build_rag_query(None) == ""

    def test_excludes_player_prefix(self):
        snapshot = {"name": "player_abc", "title": "冠军"}
        query = _build_rag_query(snapshot)
        assert "player_abc" not in query
        assert "冠军" in query


# ── gather_context_node ──


class TestGatherContext:
    @pytest.mark.asyncio
    async def test_no_tool_calls(self):
        """LLM 不调用工具时直接返回空 enriched_context。"""
        mock_response = MagicMock()
        mock_response.tool_calls = []

        mock_llm_bound = MagicMock()
        mock_llm_bound.ainvoke = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm_bound)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.create_tools", MagicMock(return_value=[])):
                state = _base_state()
                result = await gather_context_node(state)

        assert result["enriched_context"] == ""

    @pytest.mark.asyncio
    async def test_with_tool_calls(self):
        """LLM 调用工具后收集上下文。"""
        tool_response_1 = MagicMock()
        tool_response_1.tool_calls = [
            {"name": "dynamic_rag_query", "args": {"query": "PVP规则"}, "id": "tc-1"}
        ]

        tool_response_2 = MagicMock()
        tool_response_2.tool_calls = []

        mock_llm_bound = MagicMock()
        mock_llm_bound.ainvoke = AsyncMock(side_effect=[tool_response_1, tool_response_2])

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm_bound)

        mock_tool = AsyncMock()
        mock_tool.name = "dynamic_rag_query"
        mock_tool.ainvoke = AsyncMock(return_value="PVP匹配规则: 随机匹配")

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.create_tools", MagicMock(return_value=[mock_tool])):
                state = _base_state()
                result = await gather_context_node(state)

        assert "dynamic_rag_query" in result["enriched_context"]
        assert "PVP匹配规则" in result["enriched_context"]

    @pytest.mark.asyncio
    async def test_exception_in_try_block(self):
        """ainvoke 抛异常时返回错误。"""
        mock_llm_bound = MagicMock()
        mock_llm_bound.ainvoke = AsyncMock(side_effect=Exception("LLM invoke failed"))

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm_bound)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.create_tools", MagicMock(return_value=[])):
                state = _base_state()
                result = await gather_context_node(state)

        assert result["enriched_context"] == ""
        assert any("上下文收集失败" in e for e in result["errors"])


# ── behavior_analysis_node ──


class TestBehaviorAnalysis:
    @pytest.mark.asyncio
    async def test_successful_analysis(self):
        profile = BehaviorProfile(
            playstyle="competitive",
            current_goal=["level up"],
            bottlenecks=["time"],
            engagement_level="high",
        )

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=profile)

        mock_llm_structured = MagicMock()
        mock_llm_structured.__or__ = MagicMock(return_value=mock_chain)

        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(return_value=mock_llm_structured)

        mock_prompt = MagicMock()
        mock_prompt.__or__ = MagicMock(return_value=mock_chain)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template:
                mock_template.from_messages = MagicMock(return_value=mock_prompt)
                state = _base_state()
                result = await behavior_analysis_node(state)

        assert result["behavior_report"] != ""
        restored = BehaviorProfile.model_validate_json(result["behavior_report"])
        assert restored.playstyle == "competitive"

    @pytest.mark.asyncio
    async def test_chain_failure_returns_error(self):
        """chain.ainvoke 抛异常（在 try 块内）时返回错误。"""
        profile_error = Exception("structured output failed")

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=profile_error)

        mock_llm_structured = MagicMock()

        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(return_value=mock_llm_structured)

        # 让 prompt | llm_structured 返回 mock_chain
        def or_side_effect(other):
            return mock_chain

        mock_prompt = MagicMock()
        mock_prompt.__or__ = MagicMock(side_effect=or_side_effect)
        mock_llm_structured.__or__ = MagicMock(side_effect=or_side_effect)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template:
                mock_template.from_messages = MagicMock(return_value=mock_prompt)
                state = _base_state()
                result = await behavior_analysis_node(state)

        assert result["behavior_report"] == ""
        assert any("行为分析失败" in e for e in result["errors"])


# ── action_reasoning_node ──


class TestActionReasoning:
    @pytest.mark.asyncio
    async def test_successful_reasoning(self):
        actions = ActionList(actions=[
            RecommendedAction(action_type="quest", priority="high", reason="提升等级"),
        ])

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=actions)

        mock_llm_structured = MagicMock()

        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(return_value=mock_llm_structured)

        def or_side_effect(other):
            return mock_chain

        mock_prompt = MagicMock()
        mock_prompt.__or__ = MagicMock(side_effect=or_side_effect)
        mock_llm_structured.__or__ = MagicMock(side_effect=or_side_effect)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template:
                mock_template.from_messages = MagicMock(return_value=mock_prompt)
                state = _base_state(behavior_report='{"playstyle":"competitive"}')
                result = await action_reasoning_node(state)

        assert len(result["reasoned_actions"]) == 1
        assert result["reasoned_actions"][0]["action_type"] == "quest"

    @pytest.mark.asyncio
    async def test_null_result_returns_error(self):
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=None)

        mock_llm_structured = MagicMock()

        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(return_value=mock_llm_structured)

        def or_side_effect(other):
            return mock_chain

        mock_prompt = MagicMock()
        mock_prompt.__or__ = MagicMock(side_effect=or_side_effect)
        mock_llm_structured.__or__ = MagicMock(side_effect=or_side_effect)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template:
                mock_template.from_messages = MagicMock(return_value=mock_prompt)
                state = _base_state()
                result = await action_reasoning_node(state)

        assert result["reasoned_actions"] == []
        assert any("空结果" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_chain_failure_returns_error(self):
        """chain.ainvoke 抛异常（在 try 块内）时返回错误。"""
        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(side_effect=Exception("invoke failed"))

        mock_llm_structured = MagicMock()

        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(return_value=mock_llm_structured)

        def or_side_effect(other):
            return mock_chain

        mock_prompt = MagicMock()
        mock_prompt.__or__ = MagicMock(side_effect=or_side_effect)
        mock_llm_structured.__or__ = MagicMock(side_effect=or_side_effect)

        with patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)):
            with patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template:
                mock_template.from_messages = MagicMock(return_value=mock_prompt)
                state = _base_state()
                result = await action_reasoning_node(state)

        assert result["reasoned_actions"] == []
        assert any("行动推理失败" in e for e in result["errors"])


# ── merge_output_node ──


class TestMergeOutput:
    @pytest.mark.asyncio
    async def test_successful_merge(self):
        profile = BehaviorProfile(
            playstyle="competitive",
            current_goal=["level up"],
            bottlenecks=["time"],
            engagement_level="high",
        )
        actions = [RecommendedAction(action_type="quest", priority="high", reason="r")]

        state = _base_state(
            behavior_report=profile.model_dump_json(),
            reasoned_actions=[a.model_dump() for a in actions],
        )
        result = await merge_output_node(state)

        output = result["final_output"]
        assert output["player_profile"]["playstyle"] == "competitive"
        assert len(output["recommended_actions"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_behavior_report(self):
        state = _base_state(
            behavior_report="not valid json{{",
            reasoned_actions=[],
        )
        result = await merge_output_node(state)

        assert result["final_output"] == {}
        assert any("输出组装失败" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_empty_inputs(self):
        state = _base_state(
            behavior_report="",
            reasoned_actions=[],
        )
        result = await merge_output_node(state)

        assert result["final_output"] == {}
        assert "errors" in result
