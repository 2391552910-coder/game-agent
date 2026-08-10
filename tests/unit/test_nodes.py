"""LangGraph 节点单元测试。

所有外部依赖（LLM, RAG, DB）均被 mock，只测试节点逻辑。
"""

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
from src.core.agents.prompts import ACTION_REASONING_SYSTEM, ACTION_REASONING_USER


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
    async def test_successful_retrieval_logs_lightrag_timing(self, caplog):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="商业区开放时间: 9:00-22:00")

        with (
            patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)),
            caplog.at_level("INFO", logger="src.core.agents.nodes"),
        ):
            result = await retrieve_rag_context_node(_base_state())

        assert result["rag_context"] == "商业区开放时间: 9:00-22:00"
        assert "lightrag_get_elapsed_ms=" in caplog.text
        assert "lightrag_query_elapsed_ms=" in caplog.text

    @pytest.mark.asyncio
    async def test_retrieval_query_param_disables_rerank_by_default(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="商业区开放时间: 9:00-22:00")

        with patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)):
            result = await retrieve_rag_context_node(_base_state())

        assert result["rag_context"] == "商业区开放时间: 9:00-22:00"
        query_param = mock_rag.aquery.call_args.kwargs["param"]
        assert query_param.enable_rerank is False

    @pytest.mark.asyncio
    async def test_retrieval_requests_context_without_lightrag_answer_generation(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="商业区开放时间: 9:00-22:00")

        with patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)):
            result = await retrieve_rag_context_node(_base_state())

        assert result["rag_context"] == "商业区开放时间: 9:00-22:00"
        query_param = mock_rag.aquery.call_args.kwargs["param"]
        assert query_param.only_need_context is True

    @pytest.mark.asyncio
    async def test_gateway_v2_retrieval_bounds_context_before_final_prompt(self):
        mock_rag = AsyncMock()
        full_context = "RAG context entry. " * 200
        mock_rag.aquery = AsyncMock(return_value=full_context)

        with (
            patch("src.config.settings.rag_exact_match_enabled", False, create=True),
            patch("src.config.settings.llm_gateway_v2_rag_context_max_tokens", 32, create=True),
            patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)),
        ):
            result = await retrieve_rag_context_node(
                _base_state(gateway_context={"eventId": "gateway-v2-test"})
            )

        assert result["rag_context"] != full_context
        assert len(result["rag_context"]) < len(full_context)
        query_param = mock_rag.aquery.call_args.kwargs["param"]
        assert query_param.mode == "naive"
        assert query_param.top_k == 10
        assert query_param.chunk_top_k == 10
        assert query_param.max_entity_tokens == 1_500
        assert query_param.max_relation_tokens == 2_500
        assert query_param.max_total_tokens == 6_000

    @pytest.mark.asyncio
    async def test_retrieval_uses_configured_chunk_top_k_and_prepends_exact_context(self):
        mock_rag = AsyncMock()
        mock_rag.aquery = AsyncMock(return_value="向量检索内容")

        with (
            patch("src.config.settings.rag_exact_match_enabled", True, create=True),
            patch("src.config.settings.lightrag_chunk_top_k", 50, create=True),
            patch("src.core.agents.nodes.get_rag", AsyncMock(return_value=mock_rag)),
            patch(
                "src.core.agents.nodes.retrieve_exact_rag_context",
                AsyncMock(return_value="精确匹配内容"),
                create=True,
            ),
        ):
            result = await retrieve_rag_context_node(_base_state())

        query_param = mock_rag.aquery.call_args.kwargs["param"]
        assert query_param.chunk_top_k == 50
        assert result["rag_context"] == "精确匹配内容\n\n向量检索内容"

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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.create_tools", MagicMock(return_value=[])),
        ):
            state = _base_state()
            result = await gather_context_node(state)

        assert result["enriched_context"] == ""

    @pytest.mark.asyncio
    async def test_dynamic_rag_tool_is_hidden_when_disabled(self):
        """默认不把 dynamic_rag_query 暴露给 gather_context 的工具决策 LLM。"""
        mock_response = MagicMock()
        mock_response.tool_calls = []

        mock_llm_bound = MagicMock()
        mock_llm_bound.ainvoke = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm_bound)

        dynamic_tool = MagicMock()
        dynamic_tool.name = "dynamic_rag_query"
        history_tool = MagicMock()
        history_tool.name = "query_player_history"

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch(
                "src.core.agents.nodes.create_tools",
                MagicMock(return_value=[dynamic_tool, history_tool]),
            ),
        ):
            result = await gather_context_node(_base_state())

        bound_tools = mock_llm.bind_tools.call_args.args[0]
        assert result["enriched_context"] == ""
        assert [tool.name for tool in bound_tools] == ["query_player_history"]

    @pytest.mark.asyncio
    async def test_with_tool_calls(self):
        """LLM 调用工具后收集上下文。"""
        from src.config import settings

        tool_response_1 = MagicMock()
        tool_response_1.tool_calls = [{"name": "dynamic_rag_query", "args": {"query": "PVP规则"}, "id": "tc-1"}]

        tool_response_2 = MagicMock()
        tool_response_2.tool_calls = []

        mock_llm_bound = MagicMock()
        mock_llm_bound.ainvoke = AsyncMock(side_effect=[tool_response_1, tool_response_2])

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm_bound)

        mock_tool = AsyncMock()
        mock_tool.name = "dynamic_rag_query"
        mock_tool.ainvoke = AsyncMock(return_value="PVP匹配规则: 随机匹配")

        with (
            patch.object(settings, "gather_context_enable_dynamic_rag", True),
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.create_tools", MagicMock(return_value=[mock_tool])),
        ):
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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.create_tools", MagicMock(return_value=[])),
        ):
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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template,
        ):
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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template,
        ):
            mock_template.from_messages = MagicMock(return_value=mock_prompt)
            state = _base_state()
            result = await behavior_analysis_node(state)

        assert result["behavior_report"] == ""
        assert any("行为分析失败" in e for e in result["errors"])


# ── action_reasoning_node ──


class TestActionReasoning:
    def test_action_reasoning_prompt_json_examples_are_escaped(self):
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", ACTION_REASONING_SYSTEM),
                ("human", ACTION_REASONING_USER),
            ]
        )

        assert set(prompt.input_variables) == {
            "behavior_report",
            "snapshot_text",
            "rag_context",
            "enriched_context",
            "tracking_summary",
            "anomaly_text",
            "intent_result",
            "goal_evaluation_result",
            "gateway_skill_context",
        }

    def test_action_reasoning_prompt_requires_gateway_skill_output(self):
        assert "skillName" in ACTION_REASONING_SYSTEM
        assert "schemaVersion" in ACTION_REASONING_SYSTEM
        assert "arguments" in ACTION_REASONING_SYSTEM
        assert "action_type" not in ACTION_REASONING_SYSTEM

    @pytest.mark.asyncio
    async def test_successful_reasoning(self):
        actions = ActionList(
            actions=[
                RecommendedAction(skillName="observe_state", priority="high", reason="观察状态"),
            ]
        )

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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template,
        ):
            mock_template.from_messages = MagicMock(return_value=mock_prompt)
            state = _base_state(behavior_report='{"playstyle":"competitive"}')
            result = await action_reasoning_node(state)

        assert len(result["reasoned_actions"]) == 1
        assert result["reasoned_actions"][0]["skillName"] == "observe_state"
        assert result["reasoned_actions"][0]["arguments"] == {}

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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template,
        ):
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

        with (
            patch("src.core.agents.nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.nodes.ChatPromptTemplate") as mock_template,
        ):
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
        actions = [RecommendedAction(skillName="observe_state", priority="high", reason="r")]

        state = _base_state(
            behavior_report=profile.model_dump_json(),
            reasoned_actions=[a.model_dump() for a in actions],
        )
        result = await merge_output_node(state)

        output = result["final_output"]
        assert output["player_profile"]["playstyle"] == "competitive"
        assert len(output["recommended_actions"]) == 1
        assert output["recommended_actions"][0]["skillName"] == "observe_state"

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
