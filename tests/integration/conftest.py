"""PostgreSQL integration-test safety fixtures."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_PREFIX = "myagent_test_"


def _require_test_database_url() -> URL:
    raw_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if raw_dsn is None or not raw_dsn.strip():
        pytest.fail("TEST_POSTGRES_DSN must be explicitly set for integration tests")

    try:
        database_url = make_url(raw_dsn)
    except sa.exc.ArgumentError as exc:
        pytest.fail(f"TEST_POSTGRES_DSN is invalid: {exc}")

    if database_url.get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRES_DSN must use PostgreSQL")
    if database_url.database is None or not database_url.database.startswith(TEST_DATABASE_PREFIX):
        pytest.fail(f"TEST_POSTGRES_DSN database must start with {TEST_DATABASE_PREFIX!r}")

    return database_url


async def _read_current_database(database_url: URL) -> str:
    engine = create_async_engine(database_url, poolclass=sa.pool.NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(sa.text("SELECT current_database()"))
    finally:
        await engine.dispose()

    assert isinstance(value, str)
    return value


@pytest.fixture(scope="session")
def test_postgres_url() -> URL:
    """Return only an explicitly supplied, name-guarded integration database URL."""
    return _require_test_database_url()


@pytest.fixture(scope="session", autouse=True)
def verified_test_postgres_url(test_postgres_url: URL) -> URL:
    """Verify the server-selected database before any migration command can run."""
    current_database = asyncio.run(_read_current_database(test_postgres_url))
    if not current_database.startswith(TEST_DATABASE_PREFIX):
        pytest.fail(f"connected database must start with {TEST_DATABASE_PREFIX!r}")
    if current_database != test_postgres_url.database:
        pytest.fail("connected database does not match TEST_POSTGRES_DSN")
    return test_postgres_url


@pytest.fixture(scope="session")
def migration_config(verified_test_postgres_url: URL) -> Config:
    """Build an isolated Alembic config without changing process environment."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.attributes["database_url_override"] = verified_test_postgres_url.render_as_string(hide_password=False)
    return config
