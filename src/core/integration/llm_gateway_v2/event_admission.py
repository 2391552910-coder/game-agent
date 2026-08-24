from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_default_limiter: EventAdmissionLimiter | None = None
_default_limiter_config: tuple[int, float] | None = None


class EventAdmissionOverloadedError(RuntimeError):
    """The process-local event admission budget is temporarily exhausted."""


class EventAdmissionLimiter:
    """Bound database admission work before it can consume a connection."""

    def __init__(self, *, max_concurrency: int, acquire_timeout_seconds: float) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
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
                raise EventAdmissionOverloadedError("event admission capacity is saturated") from error
        finally:
            self._waiting -= 1

        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "active": self._active,
            "limit": self._max_concurrency,
            "waiting": self._waiting,
            "rejected_total": self._rejected_total,
        }


def get_default_event_admission_limiter() -> EventAdmissionLimiter:
    global _default_limiter, _default_limiter_config

    from src.config import settings

    config = (
        settings.llm_gateway_v2_event_admission_max_concurrency,
        settings.llm_gateway_v2_event_admission_acquire_timeout_seconds,
    )
    if _default_limiter is None or _default_limiter_config != config:
        _default_limiter = EventAdmissionLimiter(
            max_concurrency=config[0],
            acquire_timeout_seconds=config[1],
        )
        _default_limiter_config = config
    return _default_limiter
