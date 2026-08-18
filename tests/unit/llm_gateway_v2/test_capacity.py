from __future__ import annotations

import asyncio

import pytest

from src.core.integration.llm_gateway_v2.capacity import (
    AgentCapacityExceededError,
    AgentCapacityLimiter,
)


async def test_agent_capacity_limiter_rejects_after_bounded_wait() -> None:
    limiter = AgentCapacityLimiter(limit=1, acquire_timeout_seconds=0.01)

    async with limiter.slot():
        with pytest.raises(AgentCapacityExceededError):
            async with limiter.slot():
                raise AssertionError("the second caller must not enter the slot")

        snapshot = limiter.snapshot()
        assert snapshot.active == 1
        assert snapshot.limit == 1
        assert snapshot.rejected_total == 1

    assert limiter.snapshot().active == 0


async def test_agent_capacity_slot_is_released_when_runner_is_cancelled() -> None:
    limiter = AgentCapacityLimiter(limit=1, acquire_timeout_seconds=0.1)
    entered = asyncio.Event()

    async def hold_slot() -> None:
        async with limiter.slot():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_slot())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.snapshot().active == 0
