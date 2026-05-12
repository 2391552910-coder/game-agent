"""动态决策系统数据模型。

intent_inference、goal_evaluation、memory_update 节点的输入输出模型。
"""

from typing import Literal

from pydantic import BaseModel, Field


class InferredIntent(BaseModel):
    """本次会话意图推断结果。"""

    completed: list[str] = Field(
        default_factory=list,
        description="本次会话完成了的事情，如 ['完成了主线任务第三章', '购买了装备']",
    )
    abandoned: list[str] = Field(
        default_factory=list,
        description="本次会话中途放弃的事情，如 ['尝试了 PVP 但中途退出']",
    )
    next_likely: list[str] = Field(
        default_factory=list,
        description="下次上线最可能想做的事情（按可能性排序），如 ['继续主线任务', '强化装备']",
    )
    intent_confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="意图推断的置信度，取决于行为序列的完整性和明确程度",
    )
    session_summary: str = Field(
        default="",
        description="本次会话的简短自然语言总结，供后续节点使用",
    )


class GoalEvaluationResult(BaseModel):
    """目标校验与决策结论。"""

    has_active_goal: bool = Field(
        description="是否有正在进行的目标（来自上次分析的 player_intent 记录）",
    )
    goal_progress: float | None = Field(
        default=None,
        description="当前目标完成度，0.0 - 1.0，无历史目标时为 None",
    )
    cost_deviation: float | None = Field(
        default=None,
        description=(
            "代价偏差比，实际代价/预期代价，无历史目标时为 None。"
            "1.0 表示符合预期，>1 超出预期"
        ),
    )
    decision: Literal["continue", "downgrade", "switch", "new"] = Field(
        description=(
            "决策结论："
            "continue=继续推进原目标，"
            "downgrade=降低期望值继续，"
            "switch=切换到新目标，"
            "new=首次分析无历史目标"
        ),
    )
    decision_reason: str = Field(
        description="决策原因，说明为什么做出此判断",
    )
    feasibility_issues: list[str] = Field(
        default_factory=list,
        description="可行性问题列表，如 ['账户余额不足', '该活动已关闭']",
    )
    suggested_goal: str | None = Field(
        default=None,
        description="当 decision=switch 或 new 时，建议的新目标描述",
    )
    suggested_goal_type: str | None = Field(
        default=None,
        description="建议目标的分类标签，用于记忆系统统计",
    )


class BehaviorProfileMemory(BaseModel):
    """行为画像记忆（player_memory.behavior_profile 字段结构）。"""

    spend_tendency: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="消费倾向",
    )
    avg_spend_per_session: float = Field(
        default=0.0,
        description="每次会话平均消费金额",
    )
    preferred_content: list[str] = Field(
        default_factory=list,
        description="偏好内容类型列表，如 ['PVP', '收集', '社交']",
    )
    avg_session_minutes: float = Field(
        default=0.0,
        description="平均在线时长（分钟）",
    )


class GoalTypeStats(BaseModel):
    """单个目标类型的历史统计。"""

    total: int = Field(default=0, description="总追求次数")
    success: int = Field(default=0, description="成功次数")
    avg_cost: float = Field(default=0.0, description="平均实际代价")
    abandon_reasons: list[str] = Field(
        default_factory=list,
        description="放弃原因列表（保留最近 5 条）",
    )
