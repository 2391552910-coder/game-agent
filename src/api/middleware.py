"""
中间件: 认证 + 限流。

认证链路:
X-API-Key → Redis 缓存(TTL 5min) → PostgreSQL 验证 → 写入 request.state.tenant_id

限流:
Redis ZSET 滑动窗口, 按 IP + 租户维度。
"""

import logging
import time

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {"/health","/docs","/openapi.json","/redoc"}

class AuthMiddleware(BaseHTTPMiddleware):
    """
    API Key 认证中间件
    验证流程：
    Redis GET auth_cache"{api_key}
    命中 -> 取出 tenant_id，放行
    未命中 -> PostgreSQL 查询验证
    验证通过 -> Redis SETEX 缓存5分钟
    """
    async def dispatch(self, request: Request,call_next) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(status_code=401, content={"detail":"缺少X-API-Key"})
        # Redis 缓存查询
        from src.core.infrastructure.redis import get_redis

        redis = await get_redis()
        cache_key = f"auth_cache:{api_key}"
        cached = await redis.get(cache_key)

        if cached:
            import json
            tenant_info = json.loads(cached)
            request.state.tenant_id = tenant_info["tenant_id"]
            request.state.is_admin = tenant_info.get("is_admin", False)
            return await call_next(request)

        # PostgreSQL 验证
        from src.core.infrastructure.db import get_session

        async with get_session() as session:
            row = await session.execute(
                text("""
                    SELECT t.id, t.is_active, t.is_admin
                    FROM tenants t
                    WHERE t.api_key = :api_key
                """),
                {"api_key": api_key},
            )
            result = row.first()

        if not result or not result.is_active:
            return JSONResponse(status_code=401, content={"detail": "无效的 API Key"})

        tenant_id = str(result.id)
        tenant_info = {
            "tenant_id": tenant_id,
            "is_admin": result.is_admin,
        }

        # 写入 Redis 缓存, TTL 5 分钟
        import json

        await redis.setex(cache_key, 300, json.dumps(tenant_info))

        request.state.tenant_id = tenant_id
        request.state.is_admin = result.is_admin
        return await call_next(request)

class RateLimitMiddleware(BaseHTTPMiddleware):
      """Redis ZSET 滑动窗口限流。

      维度: IP + tenant_id
      默认: 100 req/min
      """

      def __init__(self, app, max_requests: int = 100, window: int = 60):
          super().__init__(app)
          self.max_requests = max_requests
          self.window = window

      async def dispatch(self, request: Request, call_next) -> Response:
          if request.url.path in PUBLIC_PATHS:
              return await call_next(request)

          from src.core.infrastructure.redis import get_redis

          redis = await get_redis()

          tenant_id = getattr(request.state, "tenant_id", "anonymous")
          client_ip = request.client.host if request.client else "unknown"
          key = f"ratelimit:{tenant_id}:{client_ip}"

          now = time.time()
          window_start = now - self.window

          pipe = redis.pipeline()
          pipe.zremrangebyscore(key, 0, window_start)
          pipe.zadd(key, {str(now): now})
          pipe.zcard(key)
          pipe.expire(key, self.window)
          results = await pipe.execute()

          request_count = results[2]

          if request_count > self.max_requests:
              return JSONResponse(
                  status_code=429,
                  content={"detail": "请求过于频繁，请稍后再试"},
              )

          return await call_next(request)
