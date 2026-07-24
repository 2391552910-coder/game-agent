from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

# 创建异步引擎
# pool_pre_ping=True 每次从连接池取连接前先 ping，自动剔除失效连接
# pool_size=20 连接池保持的常驻连接数
# max_overflow=40 连接池满时允许额外创建的连接数，超过则阻塞等待
# pool_recycle=3600 连接存活超过 1 小时后强制重建，防止数据库主动断开
engine = create_async_engine(
    str(settings.postgres_dsn),
    echo=False,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=40,
    pool_recycle=3600,
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


async def close_db() -> None:
    """
    关闭数据库连接池，应用关闭时调用。

    释放所有连接，确保进程干净退出。
    """
    await engine.dispose()
