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

    # 使用 Annotated 和 operator.add 确保列表是“追加”模式而非“覆盖”模式
    reasoned_actions: Annotated[list[dict], operator.add]

    final_output: dict           # merge_output 组装的最终结果

    # 错误收集也需要 reducer，否则后面的节点报错会把前面的报错冲掉
    errors: Annotated[list[str], operator.add]
