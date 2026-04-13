"""离线检测触发器。

Redis TTL Key 实现分布式去重，asyncio.create_task 执行后台分析。

机制:
- 玩家离线事件到达 → SET debounce:{user_id} {run_id} EX {TTL} NX
- SET 成功 → 启动后台分析任务
- SET 失败 → 忽略（已有分析在运行）
- 玩家重新上线 → DEL debounce:{user_id} + 取消后台任务
"""

import asyncio
import logging
import uuid

from src.config import settings

logger = logging.getLogger(__name__)

DEBOUNCE_KEY_PREFIX = "debounce:"
DEBOUNCE_TTL = settings.offline_trigger_minutes * 60  # 秒

# 跟踪后台任务，用于取消
_pending_tasks: dict[str, asyncio.Task] = {}

# 延迟初始化的编译图（无 checkpointer，后台分析不需要状态持久化）
_compiled_graph = None


async def _get_compiled_graph():
    """获取或初始化编译后的 LangGraph 图（全局缓存）。"""
    global _compiled_graph
    if _compiled_graph is None:
        from src.core.agents.orchestrator import build_orchestrator

        _compiled_graph = build_orchestrator().compile()
        logger.info("[triggers] LangGraph 图编译完成（缓存）")
    return _compiled_graph


def _debounce_key(user_id: str) -> str:
    return f"{DEBOUNCE_KEY_PREFIX}{user_id}"


async def _run_analysis_background(user_id: str, tenant_id: str, run_id: str) -> None:
    """后台执行分析流程: fetch → analyze → store。"""
    try:
        logger.info("[background] analysis start, user_id=%s, run_id=%s", user_id, run_id)

        # 1. 获取玩家快照
        from src.game_specific import fetch_player_snapshot

        snapshot = await fetch_player_snapshot(user_id)
        logger.info("[background] snapshot fetched, user_id=%s", user_id)

        # 2. 执行 LangGraph 分析（不带 checkpointer，使用缓存图）
        graph = await _get_compiled_graph()

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
            ),
            timeout=300,
        )
        output = result.get("final_output", {})
        logger.info("[background] analysis done, user_id=%s", user_id)

        # 3. 存储结果
        from src.core.infrastructure.result_store import store_analysis

        await store_analysis(tenant_id, user_id, snapshot, output)
        logger.info("[background] result stored, user_id=%s", user_id)

    except Exception as e:
        logger.error("[background] analysis failed, user_id=%s: %s", user_id, e)
    finally:
        _pending_tasks.pop(user_id, None)


async def schedule_offline_analysis(user_id: str, tenant_id: str) -> str | None:
    """调度离线分析（带去重）。

    Returns:
        run_id 如果成功调度，None 如果已有待处理的任务。
    """
    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = _debounce_key(user_id)

    # 原子 SET NX: 仅在 Key 不存在时设置，实现分布式去重
    run_id = f"flow-{uuid.uuid4().hex[:16]}"
    set_ok = await redis.set(key, run_id, ex=DEBOUNCE_TTL, nx=True)
    if not set_ok:
        existing = await redis.get(key)
        logger.info("去重忽略, user_id=%s, 已有 run=%s", user_id, existing)
        return None

    # 启动后台分析任务
    task = asyncio.create_task(_run_analysis_background(user_id, tenant_id, run_id))
    _pending_tasks[user_id] = task

    logger.info("已调度离线分析, user_id=%s, run_id=%s", user_id, run_id)
    return run_id


async def cancel_offline_analysis(user_id: str) -> None:
    """取消待处理的离线分析（玩家重新上线时调用）。"""
    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = _debounce_key(user_id)

    run_id = await redis.get(key)
    if run_id is None:
        return

    # 取消后台任务
    task = _pending_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        logger.info("已取消后台任务, user_id=%s", user_id)

    # 删除去重 Key
    await redis.delete(key)
    logger.info("已取消离线分析, user_id=%s, run_id=%s", user_id, run_id)
