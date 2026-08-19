from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from src.core.integration.llm_gateway_v2.transaction import run_database_transaction


class _DeadlockError(Exception):
    sqlstate = "40P01"


class _SerializationError(Exception):
    sqlstate = "40001"


class _Session:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object, parameters: object | None = None) -> None:
        self.statements.append((statement, parameters))

    @asynccontextmanager
    async def begin(self):
        yield


class _Factory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    @asynccontextmanager
    async def __call__(self):
        session = _Session()
        self.sessions.append(session)
        yield session


@pytest.mark.asyncio
async def test_database_transaction_retries_deadlock_in_a_new_transaction() -> None:
    factory = _Factory()
    attempts = 0
    delays: list[float] = []

    async def operation(session: _Session) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _DeadlockError()
        return "committed"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = await run_database_transaction(
        factory,
        operation,
        cycle_id=UUID("00000000-0000-0000-0000-000000000123"),
        max_attempts=3,
        base_delay_seconds=0.05,
        jitter_ratio=0.0,
        sleep=sleep,
    )

    assert result == "committed"
    assert attempts == 3
    assert len(factory.sessions) == 3
    assert delays == [0.05, 0.1]
    assert all(session.statements for session in factory.sessions)


@pytest.mark.asyncio
async def test_database_transaction_does_not_retry_non_transaction_error() -> None:
    factory = _Factory()
    attempts = 0

    async def operation(session: _Session) -> None:
        del session
        nonlocal attempts
        attempts += 1
        raise ValueError("bad data")

    with pytest.raises(ValueError, match="bad data"):
        await run_database_transaction(factory, operation, max_attempts=3)

    assert attempts == 1
