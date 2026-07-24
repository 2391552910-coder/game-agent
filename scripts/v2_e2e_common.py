from __future__ import annotations

import os

import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_PREFIX = "myagent_test_"


def require_test_database_url() -> URL:
    raw_dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not raw_dsn:
        raise RuntimeError("TEST_POSTGRES_DSN is required")
    try:
        database_url = make_url(raw_dsn)
    except sa.exc.ArgumentError as error:
        raise RuntimeError("TEST_POSTGRES_DSN is invalid") from error
    if database_url.get_backend_name() != "postgresql":
        raise RuntimeError(f"TEST_POSTGRES_DSN database must start with {TEST_DATABASE_PREFIX}")
    if database_url.database is None or not database_url.database.startswith(TEST_DATABASE_PREFIX):
        raise RuntimeError(f"TEST_POSTGRES_DSN database must start with {TEST_DATABASE_PREFIX}")
    return database_url


async def open_verified_test_engine() -> AsyncEngine:
    database_url = require_test_database_url()
    engine = create_async_engine(database_url, poolclass=sa.pool.NullPool)
    try:
        async with engine.connect() as connection:
            current_database = await connection.scalar(sa.text("SELECT current_database()"))
    except Exception:
        await engine.dispose()
        raise
    if current_database != database_url.database or not str(current_database).startswith(TEST_DATABASE_PREFIX):
        await engine.dispose()
        raise RuntimeError("connected database does not match safe TEST_POSTGRES_DSN")
    return engine
