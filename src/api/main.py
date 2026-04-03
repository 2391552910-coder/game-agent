"""FastAPI 应用入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.middleware import AuthMiddleware, RateLimitMiddleware
from src.api.routes import analysis, quota, tenants, webhooks
from src.config import settings
from src.core.infrastructure.db import close_db, init_db
from src.core.infrastructure.redis import close_redis, init_redis

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时初始化连接, 关闭时释放资源。"""
    logger.info("服务启动中...")
    await init_db()
    await init_redis()
    logger.info("所有服务初始化完成")
    yield
    logger.info("服务关闭中...")
    await close_redis()
    await close_db()
    logger.info("所有服务已关闭")


app = FastAPI(
    title="Game Agent Platform",
    version="2.0.0",
    description="多租户游戏玩家行为分析与预测平台",
    lifespan=lifespan,
)

# CORS（从 .env 读取白名单，不使用通配符）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证 + 限流中间件
app.add_middleware(RateLimitMiddleware, max_requests=100, window=60)
app.add_middleware(AuthMiddleware)

# 路由注册
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(analysis.router, prefix="/api/v1/analysis", tags=["analysis"])
app.include_router(tenants.router, prefix="/api/v1/tenants", tags=["tenants"])
app.include_router(quota.router, prefix="/api/v1/quota", tags=["quota"])


@app.get("/health")
async def health_check():
    """健康检查。"""
    return {"status": "ok", "version": "2.0.0"}
