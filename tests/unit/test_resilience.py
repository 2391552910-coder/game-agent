"""熔断器和重试机制测试。"""

import asyncio
import time

import pytest

from src.core.infrastructure.resilience import (
    CircuitBreaker,
    CircuitBreakerError,
    get_circuit_breaker,
    with_retry,
)


class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == "closed"

    def test_record_success_stays_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        # 成功重置计数器
        cb.record_failure()
        assert cb.state == "closed"

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"

        time.sleep(0.15)
        assert cb.state == "half-open"

    def test_half_open_closes_on_success(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.0)
        cb.record_failure()
        cb.record_failure()
        # 强制进入 half-open
        cb._last_failure_time = 0
        assert cb.state == "half-open"

        cb.record_success()
        assert cb.state == "closed"

    def test_execute_success(self):
        cb = CircuitBreaker("test")
        result = asyncio.get_event_loop().run_until_complete(
            cb.execute(lambda: asyncio.sleep(0, result="ok"))
        )
        assert result == "ok"
        assert cb.state == "closed"

    def test_execute_raises_when_open(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        cb.record_failure()
        assert cb.state == "open"

        with pytest.raises(CircuitBreakerError, match="已打开"):
            asyncio.get_event_loop().run_until_complete(
                cb.execute(lambda: asyncio.sleep(0))
            )

    def test_execute_tracks_failures(self):
        cb = CircuitBreaker("test", failure_threshold=2)

        async def failing():
            raise ValueError("fail")

        with pytest.raises(ValueError):
            asyncio.get_event_loop().run_until_complete(cb.execute(failing))

        assert cb.state == "closed"  # 1 failure, threshold=2

        with pytest.raises(ValueError):
            asyncio.get_event_loop().run_until_complete(cb.execute(failing))

        assert cb.state == "open"


class TestGetCircuitBreaker:
    def test_returns_same_instance(self):
        cb1 = get_circuit_breaker("shared")
        cb2 = get_circuit_breaker("shared")
        assert cb1 is cb2

    def test_different_names_different_instances(self):
        cb1 = get_circuit_breaker("a")
        cb2 = get_circuit_breaker("b")
        assert cb1 is not cb2


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        async def ok():
            nonlocal call_count
            call_count += 1
            return "done"

        result = await ok()
        assert result == "done"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("not yet")
            return "finally"

        result = await flaky()
        assert result == "finally"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        @with_retry(max_attempts=2, base_delay=0.01)
        async def always_fail():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            await always_fail()
