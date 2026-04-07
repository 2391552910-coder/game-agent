"""Prefect Flow 定义。

玩家离线分析完整流程: fetch_player_data → run_analysis → store_result
tenant_id 全程透传，确保多租户隔离。
"""

import asyncio
import logging

from prefect import flow, task

logger = logging.getLogger(__name__)

# ── 防卡死常量 ──
# 图执行总超时（秒）: 覆盖所有节点，防止整个流程无限挂起
_GRAPH_TOTAL_TIMEOUT = 300


@task(retries=2, retry_delay_seconds=10)
async def fetch_player_data(user_id: str) -> dict:
    """获取玩家快照数据。

    调用游戏方实现的 connector 获取当前状态。
    """
    from src.game_specific import fetch_player_snapshot, PlayerNotFoundError

    try:
        snapshot = await fetch_player_snapshot(user_id)
        logger.info("获取玩家数据完成, user_id=%s", user_id)
        return snapshot
    except PlayerNotFoundError:
        logger.error("玩家不存在, user_id=%s", user_id)
        raise
    except Exception as e:
        logger.error("获取玩家数据失败, user_id=%s: %s", user_id, e)
        raise


@task(retries=1, retry_delay_seconds=5)
async def run_analysis(
    user_id: str,
    tenant_id: str,
    snapshot: dict,
) -> dict:
    """调用 LangGraph 智能体执行分析。"""
    from src.core.agents.orchestrator import create_orchestrator

    graph = await create_orchestrator()

    try:
        result = await asyncio.wait_for(
            graph.ainvoke(
                {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "snapshot": snapshot,
                    "rag_context": "",
                    "enriched_context": "",
                    "behavior_report": "",
                    "reasoned_actions": [],
                    "final_output": {},
                    "errors": [],
                },
                {"configurable": {"thread_id": f"{tenant_id}:{user_id}"}},
            ),
            timeout=_GRAPH_TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("[run_analysis] 图执行超时 (%ds), user_id=%s", _GRAPH_TOTAL_TIMEOUT, user_id)
        return {"final_output": {}, "errors": [f"分析超时 ({_GRAPH_TOTAL_TIMEOUT}s)"]}

    errors = result.get("errors", [])
    if errors:
        logger.warning("分析完成但有错误, user_id=%s, errors=%s", user_id, errors)

    return result.get("final_output", {})


@task
async def store_result(
    user_id: str,
    tenant_id: str,
    snapshot: dict,
    output: dict,
) -> None:
    """持久化分析结果到 PostgreSQL。"""
    from src.core.infrastructure.result_store import store_analysis

    await store_analysis(
        tenant_id=tenant_id,
        user_id=user_id,
        snapshot=snapshot,
        output=output,
    )


@flow(
    name="player_offline_analysis",
    version="2.0",
    description="玩家离线触发的行为分析与推荐",
)
async def player_offline_analysis_flow(
    user_id: str,
    tenant_id: str,
) -> dict:
    """玩家离线分析 Flow。

    Args:
        user_id: 玩家 ID
        tenant_id: 租户 ID（非空，多租户隔离）
    """
    logger.info("开始离线分析, user_id=%s, tenant_id=%s", user_id, tenant_id)

    snapshot = await fetch_player_data(user_id)
    result = await run_analysis(user_id, tenant_id, snapshot)
    await store_result(user_id, tenant_id, snapshot, result)

    logger.info("离线分析完成, user_id=%s", user_id)
    return result
