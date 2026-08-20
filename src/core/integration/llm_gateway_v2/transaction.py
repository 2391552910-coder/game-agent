from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

import sqlalchemy as sa

T = TypeVar("T")
SessionFactory = Callable[[], Any]
TransactionOperation = Callable[[Any], Awaitable[T]]

_CYCLE_ADVISORY_LOCK = sa.text(
    "SELECT pg_advisory_xact_lock(hashtextextended(:cycle_lock_key, 0))"
)
_TRY_CYCLE_ADVISORY_LOCK = sa.text(
    "SELECT pg_try_advisory_xact_lock(hashtextextended(:cycle_lock_key, 0))"
)
logger = logging.getLogger(__name__)


def postgres_sqlstate(error: BaseException) -> str | None:
    """Return a PostgreSQL SQLSTATE from SQLAlchemy or asyncpg error wrappers."""

    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in visited:
            continue
        visited.add(marker)
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value:
                return value
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


def is_retryable_transaction_error(error: BaseException) -> bool:
    return postgres_sqlstate(error) in {"40P01", "40001"}


async def acquire_cycle_advisory_lock(session: Any, cycle_id: UUID | str) -> None:
    await session.execute(
        _CYCLE_ADVISORY_LOCK,
        {"cycle_lock_key": str(cycle_id)},
    )


async def try_acquire_cycle_advisory_lock(session: Any, cycle_id: UUID | str) -> bool:
    acquired = await session.scalar(
        _TRY_CYCLE_ADVISORY_LOCK,
        {"cycle_lock_key": str(cycle_id)},
    )
    return bool(acquired)


def retry_database_mutation(function: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Retry a repository mutation after PostgreSQL aborts its transaction."""

    @functools.wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> T:
        for attempt in range(1, 4):
            try:
                return await function(*args, **kwargs)
            except Exception as error:
                if attempt >= 3 or not is_retryable_transaction_error(error):
                    raise
                delay = _retry_delay(
                    attempt=attempt,
                    base_delay_seconds=0.05,
                    max_delay_seconds=0.2,
                    jitter_ratio=0.2,
                )
                logger.warning(
                    "Retrying Gateway V2 database transaction",
                    extra={
                        "attempt": attempt,
                        "max_attempts": 3,
                        "sqlstate": postgres_sqlstate(error),
                        "delay_ms": delay * 1_000,
                    },
                )
                await asyncio.sleep(delay)
        raise RuntimeError("database mutation retry loop did not return")

    return wrapped


def _retry_delay(
    *,
    attempt: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    jitter_ratio: float,
) -> float:
    exponential = min(max_delay_seconds, base_delay_seconds * 2 ** (attempt - 1))
    if jitter_ratio == 0:
        return exponential
    return exponential * random.uniform(1 - jitter_ratio, 1 + jitter_ratio)


async def run_database_transaction(
    session_factory: SessionFactory,
    operation: TransactionOperation[T],
    *,
    cycle_id: UUID | str | None = None,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.05,
    max_delay_seconds: float = 0.2,
    jitter_ratio: float = 0.2,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run one idempotent database mutation with bounded serialization retries.

    The callback must contain database work only. Each retry creates a fresh
    session and transaction so PostgreSQL's aborted transaction state cannot be
    reused. The cycle advisory lock is transaction-scoped and is acquired
    before repository row locks, serializing state transitions for one cycle
    while preserving concurrency between unrelated cycles.
    """

    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if base_delay_seconds <= 0 or max_delay_seconds <= 0:
        raise ValueError("transaction retry delays must be positive")
    if base_delay_seconds > max_delay_seconds:
        raise ValueError("base_delay_seconds must not exceed max_delay_seconds")
    if not 0 <= jitter_ratio <= 1:
        raise ValueError("jitter_ratio must be between zero and one")

    for attempt in range(1, max_attempts + 1):
        try:
            async with session_factory() as session, session.begin():
                if cycle_id is not None:
                    await session.execute(
                        _CYCLE_ADVISORY_LOCK,
                        {"cycle_lock_key": str(cycle_id)},
                    )
                return await operation(session)
        except Exception as error:
            if attempt >= max_attempts or not is_retryable_transaction_error(error):
                raise
            await sleep(
                _retry_delay(
                    attempt=attempt,
                    base_delay_seconds=base_delay_seconds,
                    max_delay_seconds=max_delay_seconds,
                    jitter_ratio=jitter_ratio,
                )
            )

    raise RuntimeError("database transaction retry loop did not return")
