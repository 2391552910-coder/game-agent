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
