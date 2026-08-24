from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings


def _create_engine(*, pool_size: int, max_overflow: int, pool_timeout: float):
    # pool_pre_ping 防止复用已经被 PostgreSQL/网络关闭的连接。
    return create_async_engine(
        str(settings.postgres_dsn),
        echo=False,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=3600,
    )


# Worker、outbox 和普通业务查询共享此连接池。
engine = _create_engine(
    pool_size=settings.postgres_pool_size,
    max_overflow=settings.postgres_max_overflow,
    pool_timeout=settings.postgres_pool_timeout_seconds,
)

# HTTP 事件接收使用独立池，避免模型 worker 的长事务耗尽 ACK 连接。
event_admission_engine = _create_engine(
    pool_size=settings.postgres_event_admission_pool_size,
    max_overflow=settings.postgres_event_admission_max_overflow,
    pool_timeout=settings.postgres_event_admission_pool_timeout_seconds,
)

# 异步会话工厂
# expire_on_commit=False 提交后不过期对象属性，避免异步场景下的懒加载问题
# autocommit=False 手动控制事务
# autoflush=False 手动控制 flush 时机
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

event_admission_session_factory = async_sessionmaker(
    event_admission_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话的异步上下文管理器。

    自动处理 commit 和 rollback，调用方无需手动管理事务。

    用法：
        async with get_session() as session:
            result = await session.execute(...)
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()  # 正常退出时自动提交
        except Exception:
            await session.rollback()  # 异常时自动回滚
            raise


async def init_db() -> None:
    """
    初始化数据库连接，应用启动时调用。

    执行一条简单查询验证连接是否正常，连接失败会直接抛出异常阻止启动。
    """
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    async with event_admission_engine.begin() as conn:
        await conn.execute(text("SELECT 1"))


async def close_db() -> None:
    """
    关闭数据库连接池，应用关闭时调用。

    释放所有连接，确保进程干净退出。
    """
    await event_admission_engine.dispose()
    await engine.dispose()
