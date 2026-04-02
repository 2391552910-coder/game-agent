"""
Alembic 异步迁移环境配置。

负责从 settings 读取数据库连接，并以异步方式执行迁移。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from src.config import settings

# Alembic 配置对象，提供 .ini 文件中的配置值
config = context.config

# 用 settings 中的真实 DSN 覆盖 .ini 中的占位符
# asyncpg 驱动不兼容 Alembic 同步迁移，替换为 psycopg2 驱动格式
config.set_main_option(
    "sqlalchemy.url",
    str(settings.postgres_dsn),
)

# 读取 .ini 中的日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入所有模型后赋值给 target_metadata，alembic autogenerate 才能感知表结构
# 目前为 None，Phase 1 Step 4 添加模型后更新
target_metadata = None


def run_migrations_offline() -> None:
    """
    离线模式迁移（不需要真实数据库连接）。

    适用于只生成 SQL 脚本而不直接执行的场景。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """在给定连接上执行迁移。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式迁移，使用 asyncpg 驱动连接数据库。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # 迁移不需要连接池
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式迁移入口。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()