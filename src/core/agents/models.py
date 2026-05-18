from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal[
    "observe_current_state",
    "move_to_location",
    "stop_moving",
    "jump",
    "play_basic_action",
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

    goal_metric / goal_value / expected_hours 为可选追踪字段。
    LLM 在能明确量化完成条件时填写，否则留空。
    tracking_update_node 只处理有 goal_metric 的行动。
    """
    action_type: ActionType = Field(description="第一版允许的基础行为动作类型")
    priority: Literal["high", "medium", "low"]
    reason: str = Field(description="推荐原因")
    payload: dict = Field(
        description=(
            "执行参数。observe_current_state/stop_moving 可为空；"
            "move_to_location 需要 location_id 或 position；"
            "jump 可包含 count；play_basic_action 需要 action_id。"
        ),
        default_factory=dict,
    )

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


class PlayerAnalysisOutput(BaseModel):
    """最终输出"""
    player_profile: BehaviorProfile
    recommended_actions: list[RecommendedAction]


class ActionList(BaseModel):
    """行动推理输出包装。用于 with_structured_output，避免 list[...] 不兼容。"""
    actions: list[RecommendedAction] = Field(description="推荐行动列表")
