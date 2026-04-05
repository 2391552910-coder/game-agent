from typing import Literal

from pydantic import BaseModel, Field


# 可自定义
class BehaviorProfile(BaseModel):
    """行为分析输出"""
    playstyle:str = Field(description="用户行为风格")
    current_goal: list[str] = Field(description="用户当前目标")
    bottlenecks: list[str] = Field(description="遇到的瓶颈")
    engagement_level: Literal["high", "medium", "low"]

class RecommendedAction(BaseModel):
    """推荐行动"""
    action_type : str = Field(description="行为类型")
    priority: Literal["high", "medium","low"]
    reason : str = Field(description="推荐原因")
    payload : dict = Field(description="执行参数", default_factory=dict)

class PlayerAnalysisOutput(BaseModel):
    """最终输出"""
    player_profile: BehaviorProfile
    recommended_actions: list[RecommendedAction]


class ActionList(BaseModel):
    """行动推理输出包装。用于 with_structured_output，避免 list[...] 不兼容。"""
    actions: list[RecommendedAction] = Field(description="推荐行动列表")
