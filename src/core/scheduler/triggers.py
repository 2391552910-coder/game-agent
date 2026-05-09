"""离线检测触发器。

Redis TTL Key 实现分布式去重，Prefect Deployment 执行后台分析。

机制:
- 玩家离线事件到达 → SET debounce:{user_id} {flow_run_id} EX {TTL} NX
- SET 成功 → 提交 Prefect Flow Run
- SET 失败 → 忽略（已有分析在运行）
- 玩家重新上线 → DEL debounce:{user_id} + 取消 Prefect Flow Run
"""

import logging
import uuid

from src.config import settings

logger = logging.getLogger(__name__)

DEBOUNCE_KEY_PREFIX = "debounce:"
DEBOUNCE_TTL = settings.offline_trigger_minutes * 60


def _debounce_key(user_id: str) -> str:
    return f"{DEBOUNCE_KEY_PREFIX}{user_id}"


async def schedule_offline_analysis(user_id: str, tenant_id: str) -> str | None:
    """调度离线分析（带去重）。

    Returns:
        flow_run_id 如果成功调度，None 如果已有待处理的任务。
    """
    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = _debounce_key(user_id)

    # 原子 SET NX: 仅在 Key 不存在时设置，实现分布式去重
    # 先用占位符占位，提交成功后更新为真实 flow_run_id
    placeholder = f"pending-{uuid.uuid4().hex[:8]}"
    set_ok = await redis.set(key, placeholder, ex=DEBOUNCE_TTL, nx=True)
    if not set_ok:
        existing = await redis.get(key)
        logger.info("去重忽略, user_id=%s, 已有 run=%s", user_id, existing)
        return None

    # 提交 Prefect Deployment Run
    try:
        from prefect.deployments import run_deployment

        flow_run = await run_deployment(
            name="analysis_flow/offline-analysis",
            parameters={"user_id": user_id, "tenant_id": tenant_id},
            timeout=0,  # 不等待执行完成，立即返回
        )
        flow_run_id = str(flow_run.id)

        # 更新 Redis Key 为真实 flow_run_id，便于后续取消
        await redis.set(key, flow_run_id, ex=DEBOUNCE_TTL)
        logger.info("已调度离线分析, user_id=%s, flow_run_id=%s", user_id, flow_run_id)
        return flow_run_id

    except Exception as e:
        # 提交失败时清除去重 Key，允许下次重试
        await redis.delete(key)
        logger.error("提交 Prefect Flow Run 失败, user_id=%s: %s", user_id, e)
        raise


async def cancel_offline_analysis(user_id: str) -> None:
    """取消待处理的离线分析（玩家重新上线时调用）。"""
    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = _debounce_key(user_id)

    flow_run_id = await redis.get(key)
    if flow_run_id is None:
        return

    # 删除去重 Key
    await redis.delete(key)
    logger.info("已删除去重 Key, user_id=%s", user_id)

    # 跳过占位符（提交尚未完成）
    if flow_run_id.startswith("pending-"):
        logger.info("Flow Run 尚未提交完成，跳过取消, user_id=%s", user_id)
        return

    # 取消 Prefect Flow Run
    try:
        from prefect import get_client

        async with get_client() as client:
            await client.cancel_flow_run(uuid.UUID(flow_run_id))
            logger.info("已取消 Flow Run, user_id=%s, flow_run_id=%s", user_id, flow_run_id)
    except Exception as e:
        logger.warning("取消 Flow Run 失败（可能已完成）, user_id=%s: %s", user_id, e)
