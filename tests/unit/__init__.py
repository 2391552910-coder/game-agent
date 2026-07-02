"""Pydantic 模型验证测试。"""

import pytest
from pydantic import ValidationError

from src.core.agents.models import (
    ActionList,
    BehaviorProfile,
    PlayerAnalysisOutput,
    RecommendedAction,
)
from src.core.llm.models import (
    LLMProviderConfig,
    LLMProviderCreate,
    LLMProviderResponse,
    LLMProviderUpdate,
)


# ── Agent Models ──


class TestBehaviorProfile:
    def test_valid_profile(self):
        p = BehaviorProfile(
            playstyle="competitive",
            current_goal=["level up", "get gear"],
            bottlenecks=["time limited"],
            engagement_level="high",
        )
        assert p.playstyle == "competitive"
        assert len(p.current_goal) == 2
        assert p.engagement_level == "high"

    def test_engagement_level_must_be_literal(self):
        with pytest.raises(Exception):
            BehaviorProfile(
                playstyle="casual",
                current_goal=[],
                bottlenecks=[],
                engagement_level="super_high",
            )

    def test_empty_lists_allowed(self):
        p = BehaviorProfile(
            playstyle="explorer",
            current_goal=[],
            bottlenecks=[],
            engagement_level="medium",
        )
        assert p.current_goal == []

    def test_serialization(self):
        p = BehaviorProfile(
            playstyle="social",
            current_goal=["make friends"],
            bottlenecks=[],
            engagement_level="low",
        )
        data = p.model_dump()
        assert data["playstyle"] == "social"
        assert isinstance(data["current_goal"], list)

    def test_json_round_trip(self):
        p = BehaviorProfile(
            playstyle="competitive",
            current_goal=["rank up"],
            bottlenecks=["gear gap"],
            engagement_level="high",
        )
        json_str = p.model_dump_json()
        restored = BehaviorProfile.model_validate_json(json_str)
        assert restored.playstyle == p.playstyle


class TestRecommendedAction:
    def test_valid_gateway_skill_action(self):
        a = RecommendedAction(
            skillName="move_to",
            schemaVersion="v1",
            arguments={
                "target": {"x": 61.3, "y": 0.94, "z": 154.0},
                "stopDistance": 0.5,
            },
            priority="high",
            reason="前往目标点",
        )
        assert a.skill_name == "move_to"
        assert a.schema_version == "v1"
        assert a.model_dump()["skillName"] == "move_to"
        assert a.priority == "high"
        assert a.arguments["target"]["x"] == 61.3

    def test_default_arguments(self):
        a = RecommendedAction(
            skillName="observe_state",
            priority="medium",
            reason="提示",
        )
        assert a.arguments == {}

    def test_invalid_priority(self):
        with pytest.raises(ValidationError):
            RecommendedAction(
                skillName="observe_state",
                priority="urgent",
                reason="test",
            )


class TestPlayerAnalysisOutput:
    def test_full_output(self):
        profile = BehaviorProfile(
            playstyle="competitive",
            current_goal=["level up"],
            bottlenecks=["time"],
            engagement_level="high",
        )
        actions = [
            RecommendedAction(skillName="observe_state", priority="high", reason="r1"),
            RecommendedAction(skillName="jump", priority="medium", reason="r2"),
        ]
        output = PlayerAnalysisOutput(
            player_profile=profile,
            recommended_actions=actions,
        )
        assert len(output.recommended_actions) == 2

    def test_nested_serialization(self):
        profile = BehaviorProfile(
            playstyle="explorer",
            current_goal=[],
            bottlenecks=[],
            engagement_level="medium",
        )
        output = PlayerAnalysisOutput(
            player_profile=profile,
            recommended_actions=[],
        )
        data = output.model_dump(mode="json")
        assert data["player_profile"]["playstyle"] == "explorer"
        assert data["recommended_actions"] == []


class TestActionList:
    def test_action_list(self):
        actions = [
            RecommendedAction(skillName="observe_state", priority="high", reason="r1"),
        ]
        al = ActionList(actions=actions)
        assert len(al.actions) == 1

    def test_empty_action_list(self):
        al = ActionList(actions=[])
        assert al.actions == []


# ── LLM Provider Models ──


class TestLLMProviderConfig:
    def test_valid_config(self):
        c = LLMProviderConfig(
            id="test-id",
            name="Test",
            provider="deepseek",
            model="deepseek-chat",
            api_key="sk-xxx",
            base_url="https://api.deepseek.com/v1",
            weight=2,
            model_type="default",
        )
        assert c.weight == 2

    def test_weight_must_be_positive(self):
        with pytest.raises(Exception):
            LLMProviderConfig(
                id="test-id",
                name="Test",
                provider="deepseek",
                model="deepseek-chat",
                api_key="sk-xxx",
                base_url="https://api.deepseek.com/v1",
                weight=0,
            )

    def test_negative_weight(self):
        with pytest.raises(Exception):
            LLMProviderConfig(
                id="test-id",
                name="Test",
                provider="deepseek",
                model="deepseek-chat",
                api_key="sk-xxx",
                base_url="https://api.deepseek.com/v1",
                weight=-1,
            )


class TestLLMProviderCreate:
    def test_valid_create(self):
        c = LLMProviderCreate(
            name="Test Provider",
            provider="openai",
            model="gpt-4",
            api_key="sk-xxx",
            base_url="https://api.openai.com/v1",
        )
        assert c.weight == 1
        assert c.model_type == "default"

    def test_invalid_model_type(self):
        with pytest.raises(Exception):
            LLMProviderCreate(
                name="Test",
                provider="openai",
                model="gpt-4",
                api_key="sk-xxx",
                base_url="https://api.openai.com/v1",
                model_type="slow",
            )

    def test_empty_name_rejected(self):
        with pytest.raises(Exception):
            LLMProviderCreate(
                name="",
                provider="openai",
                model="gpt-4",
                api_key="sk-xxx",
                base_url="https://api.openai.com/v1",
            )

    def test_custom_weight(self):
        c = LLMProviderCreate(
            name="Test",
            provider="openai",
            model="gpt-4",
            api_key="sk-xxx",
            base_url="https://api.openai.com/v1",
            weight=5,
        )
        assert c.weight == 5


class TestLLMProviderUpdate:
    def test_all_none_is_valid(self):
        u = LLMProviderUpdate()
        data = u.model_dump(exclude_none=True)
        assert data == {}

    def test_partial_update(self):
        u = LLMProviderUpdate(weight=3, is_active=False)
        data = u.model_dump(exclude_none=True)
        assert data == {"weight": 3, "is_active": False}

    def test_invalid_weight(self):
        with pytest.raises(Exception):
            LLMProviderUpdate(weight=0)
