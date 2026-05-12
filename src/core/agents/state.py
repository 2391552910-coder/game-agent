import operator
from typing import Annotated

from typing_extensions import TypedDict


class AnalysisState(TypedDict):
    user_id: str
    tenant_id: str               # 确保非空，从上游透传
    snapshot: dict               # 玩家快照
    rag_context: str             # 主图统一检索注入
    enriched_context: str        # 工具收集的额外上下文（历史趋势等）
    behavior_report: str         # behavior_analysis 节点输出

    # 使用 Annotated 和 operator.add 确保列表是”追加”模式而非”覆盖”模式
    reasoned_actions: Annotated[list[dict], operator.add]

    final_output: dict           # merge_output 组装的最终结果

    # 错误收集也需要 reducer，否则后面的节点报错会把前面的报错冲掉
    errors: Annotated[list[str], operator.add]

    # ── 监督机制字段 ──
    # get_action_tracking 工具写入，action_reasoning 节点读取
    # 包含上次推荐行动的完成状态摘要，格式为可读文本
    tracking_summary: str

    # detect_anomaly 工具写入，action_reasoning 节点读取
    # 每条为一个异常描述，如 “活跃度骤降: engagement high→low”
    anomalies: Annotated[list[str], operator.add]

    # get_action_tracking 工具写入，tracking_update_node 读取
    # LLM 判断与当前快照行为方向冲突、已被放弃的追踪记录 ID 列表
    abandoned_tracking_ids: Annotated[list[str], operator.add]

    # ── 动态决策系统字段 ──
    # intent_inference 节点写入，goal_evaluation 节点读取
    # 本次会话意图推断结果（InferredIntent.model_dump()）
    intent_result: dict

    # goal_evaluation 节点写入，action_reasoning 节点读取
    # 目标校验与决策结论（GoalEvaluationResult.model_dump()）
    goal_evaluation_result: dict

    # memory_update 节点读取，从 DB 加载的玩家记忆
    # behavior_profile + goal_history 合并字典
    player_memory: dict
