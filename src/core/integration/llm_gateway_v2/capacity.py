from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


class AgentCapacityExceededError(RuntimeError):
    """The bounded Agent execution pool could not accept work in time."""


@dataclass(frozen=True)
class AgentCapacitySnapshot:
    active: int
    limit: int
    waiting: int
    rejected_total: int


class AgentCapacityLimiter:
    """Shared bounded capacity for all model-backed Gateway V2 work."""

    def __init__(self, *, limit: int, acquire_timeout_seconds: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")
        self._semaphore = asyncio.Semaphore(limit)
        self._limit = limit
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._active = 0
        self._waiting = 0
        self._rejected_total = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=self._acquire_timeout_seconds,
                )
            except TimeoutError as error:
                self._rejected_total += 1
                raise AgentCapacityExceededError("agent capacity is saturated") from error
        finally:
            self._waiting -= 1

        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    def snapshot(self) -> AgentCapacitySnapshot:
        return AgentCapacitySnapshot(
            active=self._active,
            limit=self._limit,
            waiting=self._waiting,
            rejected_total=self._rejected_total,
        )
