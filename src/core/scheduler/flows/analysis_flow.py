"""Prefect Flow: 离线玩家分析。

Flow 结构:
    analysis_flow
        └── fetch_snapshot_task   获取玩家快照（游戏服务器已推送时跳过）
        └── run_agent_task        执行 LangGraph 分析
        └── store_result_task     持久化分析结果

重试策略: 最多 2 次，间隔 30 秒（保守重试）。
"""

import logging

from prefect import flow, task
from prefect.logging import get_run_logger

FLOW_NAME = "offline-analysis"
DEPLOYMENT_NAME = f"analysis_flow/{FLOW_NAME}"


@task(
    name="fetch-snapshot",
    retries=2,
    retry_delay_seconds=30,
    task_run_name="fetch-snapshot-{user_id}",
)
async def fetch_snapshot_task(user_id: str) -> dict:
    logger = get_run_logger()
    from src.game_specific import fetch_player_snapshot

    snapshot = await fetch_player_snapshot(user_id)
    logger.info("快照获取完成, user_id=%s", user_id)
    return dict(snapshot)


@task(
    name="run-agent",
    retries=2,
    retry_delay_seconds=30,
    task_run_name="run-agent-{user_id}",
)
async def run_agent_task(user_id: str, tenant_id: str, snapshot: dict) -> dict:
    import asyncio

    logger = get_run_logger()
    from src.core.agents.orchestrator import build_orchestrator

    graph = build_orchestrator().compile()
    result = await asyncio.wait_for(
        graph.ainvoke({
            "user_id": user_id,
            "tenant_id": tenant_id,
            "snapshot": snapshot,
            "rag_context": "",
            "enriched_context": "",
            "behavior_report": "",
            "reasoned_actions": [],
            "final_output": {},
            "errors": [],
            "tracking_summary": "",
            "anomalies": [],
            "abandoned_tracking_ids": [],
        }),
        timeout=300,
    )
    output = result.get("final_output", {})
    logger.info("Agent 分析完成, user_id=%s", user_id)
    return output


@task(
    name="store-result",
    retries=2,
    retry_delay_seconds=30,
    task_run_name="store-result-{user_id}",
)
async def store_result_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None:
    logger = get_run_logger()
    from src.core.infrastructure.result_store import store_analysis

    await store_analysis(tenant_id, user_id, snapshot, output)
    logger.info("分析结果已存储, user_id=%s", user_id)


@flow(
    name=FLOW_NAME,
    description="玩家离线行为分析流程",
    retries=2,
    retry_delay_seconds=30,
    log_prints=True,
)
async def analysis_flow(user_id: str, tenant_id: str, snapshot: dict | None = None) -> None:
    """玩家离线分析主流程。

    由 Prefect Worker 执行，FastAPI 进程通过 run_deployment() 提交。

    Args:
        user_id:   玩家 ID
        tenant_id: 租户 ID
        snapshot:  游戏服务器推送的快照（可选）。
                   有值时直接使用，无需再从游戏数据库拉取。
    """
    logger = get_run_logger()
    logger.info("分析流程启动, user_id=%s, tenant_id=%s", user_id, tenant_id)

    if snapshot:
        logger.info("使用游戏服务器推送的快照, user_id=%s", user_id)
    else:
        logger.info("快照未推送，主动拉取, user_id=%s", user_id)
        snapshot = await fetch_snapshot_task(user_id=user_id)

    output = await run_agent_task(user_id=user_id, tenant_id=tenant_id, snapshot=snapshot)
    await store_result_task(tenant_id=tenant_id, user_id=user_id, snapshot=snapshot, output=output)

    logger.info("分析流程完成, user_id=%s", user_id)
