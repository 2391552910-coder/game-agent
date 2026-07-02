from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GatewaySkillName = Literal[
    "observe_state",
    "move_to",
    "stop_move",
    "jump",
    "play_action",
]


# 可自定义
class BehaviorProfile(BaseModel):
    """行为分析输出"""
    playstyle: str = Field(description="用户行为风格")
    current_goal: list[str] = Field(description="用户当前目标")
    bottlenecks: list[str] = Field(description="遇到的瓶颈")
    engagement_level: Literal["high", "medium", "low"]


class RecommendedAction(BaseModel):
    """推荐行动。

    输出格式直接对齐 AiRobotGateway 的 skill 控制请求。
    goal_metric / goal_value / expected_hours 为可选追踪字段。
    LLM 在能明确量化完成条件时填写，否则留空。
    tracking_update_node 只处理有 goal_metric 的行动。
    """
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    skill_name: GatewaySkillName = Field(alias="skillName", description="AiRobotGateway skill 名称")
    schema_version: Literal["v1"] = Field(default="v1", alias="schemaVersion", description="skill 参数 schema 版本")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "AiRobotGateway skill 参数。observe_state/stop_move/jump 使用空对象；"
            "move_to 需要 target: {x,y,z}，可包含 stopDistance；"
            "play_action 需要 action。"
        ),
    )
    reason: str = Field(description="推荐原因")
    priority: Literal["high", "medium", "low"]
    ttl_ms: int | None = Field(default=30000, alias="ttlMs", gt=0, description="skill 请求有效期，单位毫秒")

    # ── 监督机制追踪字段（可选）──
    goal_metric: str | None = Field(
        default=None,
        description=(
            "完成判断指标，对应玩家快照中的数值字段名。"
            "例如：learning_courses、shopping_count、play_hours。"
            "只在行动有明确可量化目标时填写。"
        ),
    )
    goal_value: float | None = Field(
        default=None,
        description="目标值，快照中 goal_metric 字段达到此值时视为完成。",
    )
    expected_hours: int | None = Field(
        default=None,
        description="预计完成所需小时数，用于计算截止时间。不填则不设截止。",
    )

    @model_validator(mode="after")
    def validate_skill_arguments(self) -> "RecommendedAction":
        """校验第一阶段开放 skill 的关键参数。"""
        if self.skill_name == "move_to":
            target = self.arguments.get("target")
            if not isinstance(target, dict):
                raise ValueError("move_to.arguments.target 必须是包含 x/y/z 的对象")
            for axis in ("x", "y", "z"):
                value = target.get(axis)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"move_to.arguments.target.{axis} 必须是数字")

        if self.skill_name == "play_action" and "action" not in self.arguments:
            raise ValueError("play_action.arguments.action 必填")

        return self


class PlayerAnalysisOutput(BaseModel):
    """最终输出"""
    player_profile: BehaviorProfile
    recommended_actions: list[RecommendedAction]


class ActionList(BaseModel):
    """行动推理输出包装。用于 with_structured_output，避免 list[...] 不兼容。"""
    actions: list[RecommendedAction] = Field(description="推荐行动列表")
