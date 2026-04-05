"""主协调图 — Orchestrator。

图结构:
START → fetch_snapshot → retrieve_rag_context → gather_context
→ behavior_analysis → action_reasoning → merge_output → END

Checkpointer: PostgresSaver，状态持久化到 PostgreSQL。
"""

import logging

from langgraph.graph import END, START, StateGraph

from src.core.agents.nodes import (
    action_reasoning_node,
    behavior_analysis_node,
    fetch_snapshot_node,
    gather_context_node,
    merge_output_node,
    retrieve_rag_context_node,
)
from src.core.agents.state import AnalysisState

logger = logging.getLogger(__name__)


def build_orchestrator() -> StateGraph:
    """构建主协调图(不含checkpointer)"""
    builder = StateGraph(AnalysisState)

    # 注册节点
    builder.add_node("fetch_snapshot", fetch_snapshot_node)
    builder.add_node("retrieve_rag_context", retrieve_rag_context_node)
    builder.add_node("gather_context", gather_context_node)
    builder.add_node("behavior_analysis", behavior_analysis_node)
    builder.add_node("action_reasoning", action_reasoning_node)
    builder.add_node("merge_output", merge_output_node)

    # 线性边
    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "retrieve_rag_context")
    builder.add_edge("retrieve_rag_context", "gather_context")
    builder.add_edge("gather_context", "behavior_analysis")
    builder.add_edge("behavior_analysis", "action_reasoning")
    builder.add_edge("action_reasoning", "merge_output")
    builder.add_edge("merge_output", END)

    return builder


async def create_orchestrator():
    """创建带 PostgresSaver checkpointer 的编译图。

    用法:
        graph = await create_orchestrator()
        result = await graph.ainvoke(
            {"user_id": "...", "tenant_id": "...", ...},
            {"configurable": {"thread_id": "user_123"}},
        )
    """
    builder = build_orchestrator()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from src.config import settings

    checkpointer = AsyncPostgresSaver.from_conn_string(str(settings.postgres_dsn))
    await checkpointer.setup()

    graph = builder.compile(checkpointer=checkpointer)
    logger.info("[orchestrator] 主图编译完成, checkpointer=PostgresSaver")
    return graph
