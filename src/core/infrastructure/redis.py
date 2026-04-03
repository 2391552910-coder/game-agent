"""Redis 异步连接池。

供 LightRAG KV 存储、认证缓存、防抖 Key、限流 ZSET 使用。
"""

import redis.asyncio as redis

from src.config import settings

_pool: redis.Redis | None = None


async def init_redis() -> redis.Redis:
    """初始化 Redis 连接池。应用启动时调用。"""
    global _pool
    _pool = redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
        socket_timeout=60,
        socket_connect_timeout=30,
        retry_on_timeout=True,
    )
    await _pool.ping()
    return _pool


async def get_redis() -> redis.Redis:
    """获取 Redis 连接。未初始化时自动初始化。"""
    global _pool
    if _pool is None:
        _pool = await init_redis()
    return _pool


async def close_redis() -> None:
    """关闭连接池。应用关闭时调用。"""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
