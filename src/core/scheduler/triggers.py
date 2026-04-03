"""离线检测触发器。

Redis TTL Key + Prefect delayed flow 实现分布式防抖。

机制:
- 玩家离线事件到达 → SET debounce:{user_id} {flow_run_id} EX {TTL} NX
- SET 成功 → 提交延迟 Flow
- SET 失败 → 忽略（已有 Flow 在等待）
- 玩家重新上线 → DEL debounce:{user_id} + 取消 Flow
"""

import logging

from src.config import settings

logger = logging.getLogger(__name__)

DEBOUNCE_KEY_PREFIX = "debounce:"
DEBOUNCE_TTL = settings.offline_trigger_minutes * 60  # 秒


def _debounce_key(user_id: str) -> str:
    return f"{DEBOUNCE_KEY_PREFIX}{user_id}"


async def schedule_offline_analysis(user_id: str, tenant_id: str) -> str | None:
    """调度离线分析（带防抖）。

    Returns:
        flow_run_id 如果成功调度，None 如果已有待处理的任务。
    """

    from prefect.client import get_client

    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = _debounce_key(user_id)

    # 先提交 Prefect Flow，拿到 run_id
    async with get_client() as client:

        flow_run = await client.create_flow_run(
            name=f"offline-{user_id}",
            flow_name="player_offline_analysis",
            parameters={"user_id": user_id, "tenant_id": tenant_id},
            scheduled_start_time=None,  # 延迟执行
        )
        run_id = str(flow_run.id)

    # NX: 仅在 Key 不存在时设置，实现分布式防抖
    set_ok = await redis.set(key, run_id, ex=DEBOUNCE_TTL, nx=True)
    if not set_ok:
        # 已有待处理的任务，取消刚提交的 Flow
        async with get_client() as client:
            await client.delete_flow_run(flow_run.id)
        existing_run_id = await redis.get(key)
        logger.info(
            "防抖忽略, user_id=%s, 已有 flow_run=%s", user_id, existing_run_id
        )
        return None

    logger.info(
        "已调度离线分析, user_id=%s, flow_run=%s, 延迟=%ds",
        user_id,
        run_id,
        DEBOUNCE_TTL,
    )
    return run_id


async def cancel_offline_analysis(user_id: str) -> None:
    """取消待处理的离线分析（玩家重新上线时调用）。"""
    from prefect.client import get_client

    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = _debounce_key(user_id)

    run_id = await redis.get(key)
    if run_id is None:
        return

    # 取消 Prefect Flow Run
    async with get_client() as client:
        await client.set_flow_run_state(
            flow_run_id=run_id,
            state="Cancelled",
        )

    # 删除防抖 Key
    await redis.delete(key)
    logger.info("已取消离线分析, user_id=%s, flow_run=%s", user_id, run_id)
