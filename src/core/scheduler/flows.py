"""Prefect Flow 定义。

玩家离线分析完整流程: fetch_player_data → run_analysis → store_result
tenant_id 全程透传，确保多租户隔离。
"""

import logging

from prefect import flow, task

logger = logging.getLogger(__name__)


@task(retries=2, retry_delay_seconds=10)
async def fetch_player_data(user_id: str) -> dict:
    """获取玩家快照数据。

    当前返回 Mock 数据，后续对接游戏数据库时替换。
    """
    # TODO: 对接 src/game_specific/connector.py
    snapshot = {
        "user_id": user_id,
        "player_name": f"Player_{user_id[:6]}",
        "level": 25,
        "guild": "测试公会",
        "stats": {
            "play_hours": 120,
            "quests_completed": 45,
        },
    }
    logger.info("获取玩家数据完成, user_id=%s", user_id)
    return snapshot


@task(retries=1, retry_delay_seconds=5)
async def run_analysis(
    user_id: str,
    tenant_id: str,
    snapshot: dict,
) -> dict:
    """调用 LangGraph 智能体执行分析。"""
    from src.core.agents.orchestrator import create_orchestrator

    graph = await create_orchestrator()

    result = await graph.ainvoke(
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "snapshot": snapshot,
            "rag_context": "",
            "behavior_report": "",
            "reasoned_actions": [],
            "final_output": {},
            "errors": [],
        },
        {"configurable": {"thread_id": f"{tenant_id}:{user_id}"}},
    )

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
