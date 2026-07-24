"""动态决策节点单元测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.agents.decision_models import GoalEvaluationResult, InferredIntent

# ── intent_inference_node ──


class TestIntentInferenceNode:
    @pytest.mark.asyncio
    async def test_no_session_events_returns_low_confidence(self):
        """无会话事件时，节点正常返回低置信度意图，不抛异常"""
        from src.core.agents.decision_nodes import intent_inference_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {"level": 10},
            "player_memory": {},
        }

        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(return_value=mock_llm)
        mock_llm.ainvoke = AsyncMock(
            return_value=InferredIntent(
                session_summary="无行为数据",
                intent_confidence="low",
            )
        )

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(
            return_value=InferredIntent(
                session_summary="无行为数据",
                intent_confidence="low",
            )
        )

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(return_value=mock_llm)),
            patch("src.core.agents.decision_nodes._load_session_events", AsyncMock(return_value=[])),
            patch("src.core.agents.decision_nodes._load_recent_intents", AsyncMock(return_value=[])),
            patch(
                "langchain_core.prompts.ChatPromptTemplate.from_messages",
                return_value=MagicMock(__or__=MagicMock(return_value=mock_chain)),
            ),
        ):
            result = await intent_inference_node(state)

        assert "intent_result" in result
        assert result["intent_result"]["intent_confidence"] == "low"

    @pytest.mark.asyncio
    async def test_with_session_events(self):
        """有会话事件时，节点正常推断意图"""
        from src.core.agents.decision_nodes import intent_inference_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {"level": 15, "gold": 500},
            "player_memory": {"behavior_profile": {"spend_tendency": "medium"}},
        }

        expected_intent = InferredIntent(
            completed=["完成主线任务"],
            next_likely=["强化装备", "参与 PVP"],
            intent_confidence="high",
            session_summary="活跃会话",
        )

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=expected_intent)

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(return_value=MagicMock())),
            patch(
                "src.core.agents.decision_nodes._load_session_events",
                AsyncMock(return_value=[{"event_type": "move"}]),
            ),
            patch("src.core.agents.decision_nodes._load_recent_intents", AsyncMock(return_value=[])),
            patch(
                "langchain_core.prompts.ChatPromptTemplate.from_messages",
                return_value=MagicMock(__or__=MagicMock(return_value=mock_chain)),
            ),
        ):
            result = await intent_inference_node(state)

        assert result["intent_result"]["intent_confidence"] == "high"
        assert result["intent_result"]["completed"] == ["完成主线任务"]

    @pytest.mark.asyncio
    async def test_llm_failure_returns_error(self):
        """LLM 调用失败时，节点返回错误且 intent_result 有默认值"""
        from src.core.agents.decision_nodes import intent_inference_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {},
            "player_memory": {},
        }

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(side_effect=Exception("LLM unavailable"))),
            patch("src.core.agents.decision_nodes._load_session_events", AsyncMock(return_value=[])),
            patch("src.core.agents.decision_nodes._load_recent_intents", AsyncMock(return_value=[])),
        ):
            result = await intent_inference_node(state)

        assert "errors" in result
        assert len(result["errors"]) > 0
        # 即使失败也有默认 intent_result
        assert "intent_result" in result
        assert result["intent_result"]["intent_confidence"] == "low"


# ── goal_evaluation_node ──


class TestGoalEvaluationNode:
    @pytest.mark.asyncio
    async def test_first_time_decision_new(self):
        """首次分析，无历史目标，decision 应为 new"""
        from src.core.agents.decision_nodes import goal_evaluation_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {"gold": 1000},
            "intent_result": {"session_summary": "首次登录", "next_likely": ["探索地图"]},
            "player_memory": {},
        }

        expected = GoalEvaluationResult(
            has_active_goal=False,
            decision="new",
            decision_reason="首次分析，无历史目标",
            suggested_goal="探索新手村，熟悉基本操作",
            suggested_goal_type="exploration",
        )

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=expected)

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(return_value=MagicMock())),
            patch("src.core.agents.decision_nodes._load_last_intent", AsyncMock(return_value=None)),
            patch(
                "langchain_core.prompts.ChatPromptTemplate.from_messages",
                return_value=MagicMock(__or__=MagicMock(return_value=mock_chain)),
            ),
        ):
            result = await goal_evaluation_node(state)

        assert "goal_evaluation_result" in result
        assert result["goal_evaluation_result"]["decision"] == "new"
        assert result["goal_evaluation_result"]["suggested_goal_type"] == "exploration"

    @pytest.mark.asyncio
    async def test_continue_existing_goal(self):
        """有历史目标且进度良好，decision 为 continue"""
        from src.core.agents.decision_nodes import goal_evaluation_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {"gold": 800},
            "intent_result": {"session_summary": "继续推进", "next_likely": ["完成任务"]},
            "player_memory": {"behavior_profile": {"spend_tendency": "medium"}},
        }

        last_intent = {
            "current_goal": "收集500积分",
            "goal_type": "collection",
            "goal_status": "active",
            "goal_progress": 0.4,
        }

        expected = GoalEvaluationResult(
            has_active_goal=True,
            goal_progress=0.6,
            cost_deviation=1.1,
            decision="continue",
            decision_reason="进度良好，代价符合预期",
        )

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=expected)

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(return_value=MagicMock())),
            patch("src.core.agents.decision_nodes._load_last_intent", AsyncMock(return_value=last_intent)),
            patch(
                "langchain_core.prompts.ChatPromptTemplate.from_messages",
                return_value=MagicMock(__or__=MagicMock(return_value=mock_chain)),
            ),
        ):
            result = await goal_evaluation_node(state)

        assert result["goal_evaluation_result"]["decision"] == "continue"
        assert result["goal_evaluation_result"]["goal_progress"] == 0.6

    @pytest.mark.asyncio
    async def test_switch_goal_high_cost(self):
        """代价严重超预期，decision 为 switch"""
        from src.core.agents.decision_nodes import goal_evaluation_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {"gold": 100},
            "intent_result": {},
            "player_memory": {"behavior_profile": {"spend_tendency": "low"}},
        }

        expected = GoalEvaluationResult(
            has_active_goal=True,
            goal_progress=0.2,
            cost_deviation=2.8,
            decision="switch",
            decision_reason="代价严重超预期且玩家消费意愿低",
            suggested_goal="完成免费日常任务",
            suggested_goal_type="daily_quest",
        )

        mock_chain = MagicMock()
        mock_chain.ainvoke = AsyncMock(return_value=expected)

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(return_value=MagicMock())),
            patch(
                "src.core.agents.decision_nodes._load_last_intent",
                AsyncMock(return_value={"current_goal": "PVP排名"}),
            ),
            patch(
                "langchain_core.prompts.ChatPromptTemplate.from_messages",
                return_value=MagicMock(__or__=MagicMock(return_value=mock_chain)),
            ),
        ):
            result = await goal_evaluation_node(state)

        assert result["goal_evaluation_result"]["decision"] == "switch"
        assert result["goal_evaluation_result"]["cost_deviation"] == 2.8

    @pytest.mark.asyncio
    async def test_llm_failure_fallback_to_new(self):
        """LLM 失败时回退到 new 决策，不崩溃"""
        from src.core.agents.decision_nodes import goal_evaluation_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {},
            "intent_result": {},
            "player_memory": {},
        }

        with (
            patch("src.core.agents.decision_nodes.get_llm", AsyncMock(side_effect=Exception("fail"))),
            patch("src.core.agents.decision_nodes._load_last_intent", AsyncMock(return_value=None)),
        ):
            result = await goal_evaluation_node(state)

        assert "errors" in result
        assert result["goal_evaluation_result"]["decision"] == "new"


# ── memory_update_node ──


class TestMemoryUpdateNode:
    @pytest.mark.asyncio
    async def test_upsert_called(self):
        """memory_update 节点调用 upsert 和 save_intent，不抛异常"""
        from src.core.agents.decision_nodes import memory_update_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {"gold": 500, "session_minutes": 45},
            "intent_result": {"session_summary": "完成任务", "next_likely": []},
            "goal_evaluation_result": {
                "decision": "continue",
                "decision_reason": "进度良好",
                "has_active_goal": True,
                "goal_progress": 0.5,
                "cost_deviation": 1.1,
                "suggested_goal_type": "quest",
            },
            "final_output": {},
            "player_memory": {},
        }

        with (
            patch("src.core.agents.decision_nodes._upsert_player_memory", AsyncMock()) as mock_upsert,
            patch("src.core.agents.decision_nodes._save_player_intent", AsyncMock()) as mock_save,
        ):
            result = await memory_update_node(state)

        mock_upsert.assert_called_once()
        mock_save.assert_called_once()
        assert "errors" not in result or result.get("errors") == []

    @pytest.mark.asyncio
    async def test_upsert_failure_returns_error(self):
        """upsert 失败时返回错误，不崩溃"""
        from src.core.agents.decision_nodes import memory_update_node

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {},
            "intent_result": {},
            "goal_evaluation_result": {},
            "final_output": {},
            "player_memory": {},
        }

        with (
            patch(
                "src.core.agents.decision_nodes._upsert_player_memory",
                AsyncMock(side_effect=Exception("DB error")),
            ),
            patch("src.core.agents.decision_nodes._save_player_intent", AsyncMock()),
        ):
            result = await memory_update_node(state)

        assert "errors" in result
        assert "记忆更新失败" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_save_intent_called_with_correct_args(self):
        """_save_player_intent 被调用时参数包含 intent_result 和 goal_eval"""
        from src.core.agents.decision_nodes import memory_update_node

        intent = {"session_summary": "测试会话", "next_likely": ["任务A"]}
        goal_eval = {
            "decision": "switch",
            "decision_reason": "切换原因",
            "has_active_goal": True,
            "goal_progress": 0.3,
            "cost_deviation": 2.5,
            "suggested_goal": "新目标",
            "suggested_goal_type": "daily_quest",
            "feasibility_issues": [],
        }

        state = {
            "user_id": "user-001",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "snapshot": {},
            "intent_result": intent,
            "goal_evaluation_result": goal_eval,
            "final_output": {},
            "player_memory": {},
        }

        with (
            patch("src.core.agents.decision_nodes._upsert_player_memory", AsyncMock()),
            patch("src.core.agents.decision_nodes._save_player_intent", AsyncMock()) as mock_save,
        ):
            await memory_update_node(state)

        call_args = mock_save.call_args
        assert call_args.kwargs["intent_result"] == intent
        assert call_args.kwargs["goal_eval"]["decision"] == "switch"


# ── 辅助函数：_update_behavior_profile ──


class TestUpdateBehaviorProfile:
    def test_first_update(self):
        """首次更新，均值等于新值"""
        from src.core.agents.decision_nodes import _update_behavior_profile

        result = _update_behavior_profile({}, {"gold_spent": 200, "session_minutes": 60}, count=1)
        assert result["avg_spend_per_session"] == 200.0
        assert result["avg_session_minutes"] == 60.0

    def test_sliding_average(self):
        """第二次更新，均值为滑动平均"""
        from src.core.agents.decision_nodes import _update_behavior_profile

        existing = {
            "avg_spend_per_session": 100.0,
            "avg_session_minutes": 30.0,
            "spend_tendency": "low",
            "preferred_content": [],
        }
        result = _update_behavior_profile(existing, {"gold_spent": 300, "session_minutes": 90}, count=2)
        # 新均值 = 100 * 1/2 + 300 * 1/2 = 200
        assert result["avg_spend_per_session"] == 200.0
        assert result["avg_session_minutes"] == 60.0

    def test_spend_tendency_high(self):
        """平均消费 > 500 时，倾向为 high"""
        from src.core.agents.decision_nodes import _update_behavior_profile

        result = _update_behavior_profile({}, {"gold_spent": 600}, count=1)
        assert result["spend_tendency"] == "high"

    def test_spend_tendency_low(self):
        """平均消费 <= 100 时，倾向为 low"""
        from src.core.agents.decision_nodes import _update_behavior_profile

        result = _update_behavior_profile({}, {"gold_spent": 50}, count=1)
        assert result["spend_tendency"] == "low"


# ── 辅助函数：_update_goal_history ──


class TestUpdateGoalHistory:
    def test_first_occurrence_not_written(self):
        """同一 goal_type 首次出现，不写入 history"""
        from src.core.agents.decision_nodes import _update_goal_history

        result = _update_goal_history({}, {"decision": "new", "suggested_goal_type": "pvp_rank"}, analysis_count=1)
        assert "pvp_rank" not in result

    def test_second_occurrence_written(self):
        """同一 goal_type 第二次出现，写入 history"""
        from src.core.agents.decision_nodes import _update_goal_history

        # 第一次：total=1，不写入
        history = _update_goal_history({}, {"decision": "new", "suggested_goal_type": "pvp_rank"}, analysis_count=1)
        assert "pvp_rank" not in history

        # 模拟第一次已计数但未写入的中间状态（实际上第一次不写入，第二次才写入）
        # 直接构造 total=1 的状态来测试第二次写入
        pre_history = {"pvp_rank": {"total": 1, "success": 0, "avg_cost": 0.0, "abandon_reasons": []}}
        result = _update_goal_history(
            pre_history,
            {"decision": "continue", "suggested_goal_type": "pvp_rank"},
            analysis_count=2,
        )
        assert "pvp_rank" in result
        assert result["pvp_rank"]["total"] == 2

    def test_no_goal_type_returns_unchanged(self):
        """无 goal_type 时，history 不变"""
        from src.core.agents.decision_nodes import _update_goal_history

        existing = {"quest": {"total": 3, "success": 2, "avg_cost": 1.2, "abandon_reasons": []}}
        result = _update_goal_history(existing, {"decision": "continue"}, analysis_count=5)
        assert result == existing

    def test_abandon_reason_appended(self):
        """switch 决策时，放弃原因被追加"""
        from src.core.agents.decision_nodes import _update_goal_history

        pre_history = {"quest": {"total": 2, "success": 1, "avg_cost": 1.0, "abandon_reasons": []}}
        result = _update_goal_history(
            pre_history,
            {"decision": "switch", "decision_reason": "代价超预期", "suggested_goal_type": "quest"},
            analysis_count=3,
        )
        assert "代价超预期" in result["quest"]["abandon_reasons"]
