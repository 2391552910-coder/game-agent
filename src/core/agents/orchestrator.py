"""主协调图 — Orchestrator。

图结构:
START → fetch_snapshot → retrieve_rag_context
      → intent_inference → goal_evaluation
      → gather_context
      → behavior_analysis → action_reasoning → merge_output
      → tracking_update → memory_update → END

Checkpointer: PostgresSaver，状态持久化到 PostgreSQL。
"""

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.agents.decision_nodes import (
    goal_evaluation_node,
    intent_inference_node,
    memory_update_node,
)
from src.core.agents.nodes import (
    action_reasoning_node,
    behavior_analysis_node,
    fetch_snapshot_node,
    gather_context_node,
    merge_output_node,
    retrieve_rag_context_node,
    tracking_update_node,
)
from src.core.agents.state import AnalysisState
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields

logger = logging.getLogger(__name__)


def _timed_node(
    node_name: str,
    node_func: Callable[[AnalysisState], Awaitable[dict[str, Any]]],
) -> Callable[[AnalysisState], Awaitable[dict[str, Any]]]:
    """为 LangGraph 节点增加统一耗时日志。"""

    async def wrapper(state: AnalysisState) -> dict[str, Any]:
        started = perf_counter()
        print(f"TIMING kind=agent_node node={node_name} status=started", flush=True)
        logger.info("[agent_node_timing] node=%s status=started", node_name)
        try:
            result = await node_func(state)
        except Exception as error:
            elapsed_ms = (perf_counter() - started) * 1000
            print(
                f"TIMING kind=agent_node node={node_name} status=failed elapsed_ms={elapsed_ms:.2f}",
                flush=True,
            )
            logger.error(
                "[agent_node_timing] node=%s status=failed elapsed_ms=%.2f",
                node_name,
                elapsed_ms,
                extra=safe_exception_fields(
                    stage="agent",
                    category="node_failed",
                    error=error,
                    elapsed_ms=elapsed_ms,
                ),
            )
            raise

        elapsed_ms = (perf_counter() - started) * 1000
        errors = result.get("errors", []) if isinstance(result, dict) else []
        print(
            (
                f"TIMING kind=agent_node node={node_name} status=completed "
                f"elapsed_ms={elapsed_ms:.2f} errors_count={len(errors)}"
            ),
            flush=True,
        )
        logger.info(
            "[agent_node_timing] node=%s status=completed elapsed_ms=%.2f errors_count=%d",
            node_name,
            elapsed_ms,
            len(errors),
        )
        return result

    return wrapper


def build_orchestrator() -> StateGraph:
    """构建主协调图（不含 checkpointer）"""
    builder = StateGraph(AnalysisState)

    # 注册节点
    builder.add_node("fetch_snapshot", _timed_node("fetch_snapshot", fetch_snapshot_node))
    builder.add_node("retrieve_rag_context", _timed_node("retrieve_rag_context", retrieve_rag_context_node))
    builder.add_node("intent_inference", _timed_node("intent_inference", intent_inference_node))
    builder.add_node("goal_evaluation", _timed_node("goal_evaluation", goal_evaluation_node))
    builder.add_node("gather_context", _timed_node("gather_context", gather_context_node))
    builder.add_node("behavior_analysis", _timed_node("behavior_analysis", behavior_analysis_node))
    builder.add_node("action_reasoning", _timed_node("action_reasoning", action_reasoning_node))
    builder.add_node("merge_output", _timed_node("merge_output", merge_output_node))
    builder.add_node("tracking_update", _timed_node("tracking_update", tracking_update_node))
    builder.add_node("memory_update", _timed_node("memory_update", memory_update_node))

    # 线性边
    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "retrieve_rag_context")
    builder.add_edge("retrieve_rag_context", "intent_inference")
    builder.add_edge("intent_inference", "goal_evaluation")
    builder.add_edge("goal_evaluation", "gather_context")
    builder.add_edge("gather_context", "behavior_analysis")
    builder.add_edge("behavior_analysis", "action_reasoning")
    builder.add_edge("action_reasoning", "merge_output")
    builder.add_edge("merge_output", "tracking_update")
    builder.add_edge("tracking_update", "memory_update")
    builder.add_edge("memory_update", END)

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
