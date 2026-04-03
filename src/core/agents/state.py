from typing_extensions import TypedDict


class AnalysisState(TypedDict):
    user_id: str
    tenant_id: str              # 确保非空，从上游透传
    snapshot: dict              # 玩家快照（从游戏服务器获取的原始数据）
    rag_context: str            # 主图统一检索注入
    behavior_report: str        # behavior_analysis 节点输出
    reasoned_actions: list[dict] # action_reasoning 节点输出
    final_output: dict          # merge_output 组装的最终结果
    errors: list[str]           # 错误收集
