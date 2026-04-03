import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class CircuitBreakerError(Exception):
    """熔断器打开异常。"""
    pass


class CircuitBreaker:
    """熔断器实现。

    三态: closed → open → half-open → closed
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._last_failure_time = 0.0
        self._state = "closed"

    @property
    def state(self) -> str:
        if self._state == "open" and time.time() - self._last_failure_time > self.recovery_timeout:
            self._state = "half-open"
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = "open"
            logger.warning(
                "熔断器 '%s' 打开, 连续失败 %d 次", self.name, self._failures
            )

    async def execute(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        if self.state == "open":
            raise CircuitBreakerError(f"熔断器 '{self.name}' 已打开")

        try:
            result = await func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise


_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str) -> CircuitBreaker:
    """获取或创建命名熔断器。"""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    """异步重试装饰器。指数退避。"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=base_delay, max=max_delay),
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)

        return wrapper

    return decorator
