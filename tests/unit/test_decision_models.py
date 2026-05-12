"""decision_models 单元测试。"""

import pytest

from src.core.agents.decision_models import (
    BehaviorProfileMemory,
    GoalEvaluationResult,
    GoalTypeStats,
    InferredIntent,
)


class TestInferredIntent:
    def test_defaults(self):
        intent = InferredIntent(session_summary="玩家本次在线约 30 分钟")
        assert intent.completed == []
        assert intent.abandoned == []
        assert intent.next_likely == []
        assert intent.intent_confidence == "medium"
        assert intent.session_summary == "玩家本次在线约 30 分钟"

    def test_full_fields(self):
        intent = InferredIntent(
            completed=["完成主线任务第三章"],
            abandoned=["尝试 PVP 但中途退出"],
            next_likely=["继续主线任务", "强化装备"],
            intent_confidence="high",
            session_summary="活跃会话，完成了主线任务",
        )
        assert len(intent.completed) == 1
        assert len(intent.next_likely) == 2
        assert intent.intent_confidence == "high"

    def test_confidence_low(self):
        intent = InferredIntent(intent_confidence="low")
        assert intent.intent_confidence == "low"

    def test_model_dump_roundtrip(self):
        intent = InferredIntent(
            completed=["任务A"],
            next_likely=["任务B"],
            intent_confidence="high",
            session_summary="测试",
        )
        data = intent.model_dump()
        restored = InferredIntent(**data)
        assert restored.completed == ["任务A"]
        assert restored.intent_confidence == "high"


class TestGoalEvaluationResult:
    def test_new_decision(self):
        result = GoalEvaluationResult(
            has_active_goal=False,
            decision="new",
            decision_reason="首次分析",
        )
        assert result.goal_progress is None
        assert result.cost_deviation is None
        assert result.feasibility_issues == []
        assert result.suggested_goal is None

    def test_continue_decision(self):
        result = GoalEvaluationResult(
            has_active_goal=True,
            goal_progress=0.6,
            cost_deviation=1.2,
            decision="continue",
            decision_reason="进度良好，代价略超预期但在可接受范围",
        )
        assert result.decision == "continue"
        assert result.goal_progress == 0.6
        assert result.suggested_goal is None

    def test_switch_decision(self):
        result = GoalEvaluationResult(
            has_active_goal=True,
            goal_progress=0.2,
            cost_deviation=2.5,
            decision="switch",
            decision_reason="代价严重超预期，玩家消费意愿低",
            suggested_goal="完成日常任务获取免费积分",
            suggested_goal_type="daily_quest",
        )
        assert result.decision == "switch"
        assert result.suggested_goal == "完成日常任务获取免费积分"
        assert result.suggested_goal_type == "daily_quest"

    def test_downgrade_decision(self):
        result = GoalEvaluationResult(
            has_active_goal=True,
            goal_progress=0.3,
            cost_deviation=1.8,
            decision="downgrade",
            decision_reason="进度慢但玩家有意愿，降低目标值",
        )
        assert result.decision == "downgrade"

    def test_feasibility_issues(self):
        result = GoalEvaluationResult(
            has_active_goal=False,
            decision="new",
            decision_reason="首次分析",
            feasibility_issues=["账户余额不足", "该活动已关闭"],
        )
        assert len(result.feasibility_issues) == 2

    def test_model_dump_roundtrip(self):
        result = GoalEvaluationResult(
            has_active_goal=True,
            goal_progress=0.5,
            decision="continue",
            decision_reason="测试",
        )
        data = result.model_dump()
        restored = GoalEvaluationResult(**data)
        assert restored.decision == "continue"
        assert restored.goal_progress == 0.5


class TestBehaviorProfileMemory:
    def test_defaults(self):
        profile = BehaviorProfileMemory()
        assert profile.spend_tendency == "medium"
        assert profile.avg_spend_per_session == 0.0
        assert profile.preferred_content == []
        assert profile.avg_session_minutes == 0.0

    def test_high_spender(self):
        profile = BehaviorProfileMemory(
            spend_tendency="high",
            avg_spend_per_session=800.0,
            preferred_content=["PVP", "收集"],
            avg_session_minutes=90.0,
        )
        assert profile.spend_tendency == "high"
        assert profile.avg_spend_per_session == 800.0

    def test_model_dump_roundtrip(self):
        profile = BehaviorProfileMemory(
            spend_tendency="low",
            avg_spend_per_session=20.0,
        )
        data = profile.model_dump()
        restored = BehaviorProfileMemory(**data)
        assert restored.spend_tendency == "low"


class TestGoalTypeStats:
    def test_defaults(self):
        stats = GoalTypeStats()
        assert stats.total == 0
        assert stats.success == 0
        assert stats.avg_cost == 0.0
        assert stats.abandon_reasons == []

    def test_with_data(self):
        stats = GoalTypeStats(
            total=5,
            success=3,
            avg_cost=1.4,
            abandon_reasons=["代价超预期", "活动关闭"],
        )
        assert stats.total == 5
        assert stats.success == 3
        assert len(stats.abandon_reasons) == 2
