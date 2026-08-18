from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock

from src.core.integration.llm_gateway_v2.capacity import (
    AgentCapacityLimiter,
    AgentCapacitySnapshot,
)

logger = logging.getLogger(__name__)

_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    10,
    25,
    50,
    100,
    250,
    500,
    1_000,
    2_500,
    5_000,
    10_000,
    20_000,
    30_000,
    45_000,
    60_000,
    120_000,
    300_000,
)


@dataclass(frozen=True)
class QueueMetrics:
    depth: int
    oldest_age_seconds: float

    def __post_init__(self) -> None:
        if self.depth < 0 or self.oldest_age_seconds < 0:
            raise ValueError("queue metrics must be non-negative")


@dataclass(frozen=True)
class LatencySnapshot:
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


@dataclass(frozen=True)
class GatewayV2RuntimeMetricsSnapshot:
    worker_active: dict[str, int]
    worker_limit: dict[str, int]
    queue_depth: dict[str, int]
    oldest_age_seconds: dict[str, float]
    dead_letters: dict[str, int]
    agent_outcomes: dict[str, int]
    callback_outcomes: dict[str, int]
    agent_latency: LatencySnapshot
    callback_latency: LatencySnapshot
    event_ack_latency: LatencySnapshot
    decision_superseded_total: int


class _LatencyHistogram:
    def __init__(self) -> None:
        self._counts = [0 for _ in _LATENCY_BUCKETS_MS]
        self._overflow = 0
        self._count = 0
        self._max_ms = 0.0

    def observe(self, elapsed_ms: float) -> None:
        if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            raise ValueError("elapsed_ms must be finite and non-negative")
        self._count += 1
        self._max_ms = max(self._max_ms, elapsed_ms)
        for index, upper_bound in enumerate(_LATENCY_BUCKETS_MS):
            if elapsed_ms <= upper_bound:
                self._counts[index] += 1
                return
        self._overflow += 1

    def snapshot(self) -> LatencySnapshot:
        return LatencySnapshot(
            count=self._count,
            p50_ms=self._percentile(0.50),
            p95_ms=self._percentile(0.95),
            p99_ms=self._percentile(0.99),
            max_ms=self._max_ms,
        )

    def _percentile(self, percentile: float) -> float:
        if self._count == 0:
            return 0.0
        target = math.ceil(self._count * percentile)
        cumulative = 0
        for upper_bound, count in zip(_LATENCY_BUCKETS_MS, self._counts, strict=True):
            cumulative += count
            if cumulative >= target:
                return upper_bound
        return self._max_ms


class GatewayV2RuntimeMetrics:
    def __init__(self, *, worker_limits: dict[str, int]) -> None:
        if not worker_limits or any(not name or limit <= 0 for name, limit in worker_limits.items()):
            raise ValueError("worker_limits must contain positive limits")
        self._lock = Lock()
        self._worker_limit = dict(worker_limits)
        self._worker_active = dict.fromkeys(worker_limits, 0)
        self._queue_depth = dict.fromkeys(worker_limits, 0)
        self._oldest_age_seconds = dict.fromkeys(worker_limits, 0.0)
        self._dead_letters = dict.fromkeys(worker_limits, 0)
        self._agent_outcomes: dict[str, int] = {}
        self._callback_outcomes: dict[str, int] = {}
        self._agent_latency = _LatencyHistogram()
        self._callback_latency = _LatencyHistogram()
        self._event_ack_latency = _LatencyHistogram()
        self._decision_superseded_total = 0

    def task_started(self, worker_type: str) -> None:
        with self._lock:
            self._require_worker(worker_type)
            if self._worker_active[worker_type] >= self._worker_limit[worker_type]:
                raise RuntimeError(f"{worker_type} worker active count exceeds configured limit")
            self._worker_active[worker_type] += 1

    def task_finished(self, worker_type: str) -> None:
        with self._lock:
            self._require_worker(worker_type)
            if self._worker_active[worker_type] <= 0:
                raise RuntimeError(f"{worker_type} worker active count is already zero")
            self._worker_active[worker_type] -= 1

    def set_queue(self, worker_type: str, queue: QueueMetrics) -> None:
        with self._lock:
            self._require_worker(worker_type)
            self._queue_depth[worker_type] = queue.depth
            self._oldest_age_seconds[worker_type] = queue.oldest_age_seconds

    def set_dead_letters(self, worker_type: str, count: int) -> None:
        if count < 0:
            raise ValueError("dead letter count must be non-negative")
        with self._lock:
            self._require_worker(worker_type)
            self._dead_letters[worker_type] = count

    def record_agent_result(self, outcome: str, *, elapsed_ms: float) -> None:
        if not outcome:
            raise ValueError("outcome must not be empty")
        with self._lock:
            self._agent_outcomes[outcome] = self._agent_outcomes.get(outcome, 0) + 1
            self._agent_latency.observe(elapsed_ms)

    def record_callback_result(self, outcome: str, *, elapsed_ms: float) -> None:
        if not outcome:
            raise ValueError("outcome must not be empty")
        with self._lock:
            self._callback_outcomes[outcome] = self._callback_outcomes.get(outcome, 0) + 1
            self._callback_latency.observe(elapsed_ms)

    def record_event_ack(self, *, elapsed_ms: float) -> None:
        with self._lock:
            self._event_ack_latency.observe(elapsed_ms)

    def record_decision_superseded(self, count: int = 1) -> None:
        if count <= 0:
            raise ValueError("superseded count must be positive")
        with self._lock:
            self._decision_superseded_total += count

    def snapshot(self) -> GatewayV2RuntimeMetricsSnapshot:
        with self._lock:
            return GatewayV2RuntimeMetricsSnapshot(
                worker_active=dict(self._worker_active),
                worker_limit=dict(self._worker_limit),
                queue_depth=dict(self._queue_depth),
                oldest_age_seconds=dict(self._oldest_age_seconds),
                dead_letters=dict(self._dead_letters),
                agent_outcomes=dict(self._agent_outcomes),
                callback_outcomes=dict(self._callback_outcomes),
                agent_latency=self._agent_latency.snapshot(),
                callback_latency=self._callback_latency.snapshot(),
                event_ack_latency=self._event_ack_latency.snapshot(),
                decision_superseded_total=self._decision_superseded_total,
            )

    def log_snapshot(self, *, agent_capacity: AgentCapacitySnapshot) -> None:
        snapshot = self.snapshot()
        logger.info(
            "LLM Gateway v2 runtime metrics: "
            "gateway_event_inbox_depth=%d gateway_event_oldest_age_seconds=%.3f "
            "gateway_event_worker_active=%d gateway_event_worker_limit=%d "
            "gateway_event_dead_letter_total=%d gateway_event_ack_calls=%d "
            "gateway_event_ack_p50_ms=%.1f gateway_event_ack_p95_ms=%.1f "
            "gateway_event_ack_p99_ms=%.1f gateway_event_ack_max_ms=%.1f "
            "event_worker_active=%d event_worker_limit=%d event_queue_depth=%d "
            "event_oldest_age_seconds=%.3f event_dead_letters=%d "
            "decision_queue_depth=%d decision_oldest_age_seconds=%.3f decision_inflight=%d "
            "decision_worker_limit=%d decision_dead_letter_total=%d "
            "decision_agent_calls=%d decision_agent_p50_ms=%.1f decision_agent_p95_ms=%.1f "
            "decision_agent_p99_ms=%.1f decision_agent_max_ms=%.1f "
            "decision_callback_calls=%d decision_callback_p50_ms=%.1f "
            "decision_callback_p95_ms=%.1f decision_callback_p99_ms=%.1f "
            "decision_callback_max_ms=%.1f decision_superseded_total=%d "
            "decision_worker_active=%d decision_worker_limit=%d decision_queue_depth=%d "
            "decision_oldest_age_seconds=%.3f decision_dead_letters=%d "
            "agent_active=%d agent_limit=%d agent_waiting=%d agent_rejected_total=%d "
            "agent_calls=%d agent_p50_ms=%.1f agent_p95_ms=%.1f agent_p99_ms=%.1f agent_max_ms=%.1f "
            "callback_calls=%d callback_p50_ms=%.1f callback_p95_ms=%.1f "
            "callback_p99_ms=%.1f callback_max_ms=%.1f "
            "agent_success=%d agent_timeout=%d agent_overloaded=%d agent_error=%d "
            "callback_accepted=%d callback_transport_error=%d callback_protocol_error=%d "
            "callback_rejected_lease_not_found=%d callback_rejected_lease_expired=%d "
            "callback_rejected_generation_mismatch=%d "
            "callback_rejected_state_version_mismatch=%d "
            "callback_rejected_session_not_running=%d callback_rejected_invalid_payload=%d "
            "callback_rejected_other=%d",
            snapshot.worker_active.get("event", 0),
            snapshot.oldest_age_seconds.get("event", 0.0),
            snapshot.worker_active.get("event", 0),
            snapshot.worker_limit.get("event", 0),
            snapshot.dead_letters.get("event", 0),
            snapshot.event_ack_latency.count,
            snapshot.event_ack_latency.p50_ms,
            snapshot.event_ack_latency.p95_ms,
            snapshot.event_ack_latency.p99_ms,
            snapshot.event_ack_latency.max_ms,
            snapshot.worker_active.get("event", 0),
            snapshot.worker_limit.get("event", 0),
            snapshot.queue_depth.get("event", 0),
            snapshot.oldest_age_seconds.get("event", 0.0),
            snapshot.dead_letters.get("event", 0),
            snapshot.queue_depth.get("decision", 0),
            snapshot.oldest_age_seconds.get("decision", 0.0),
            snapshot.worker_active.get("decision", 0),
            snapshot.worker_limit.get("decision", 0),
            snapshot.dead_letters.get("decision", 0),
            snapshot.agent_latency.count,
            snapshot.agent_latency.p50_ms,
            snapshot.agent_latency.p95_ms,
            snapshot.agent_latency.p99_ms,
            snapshot.agent_latency.max_ms,
            snapshot.callback_latency.count,
            snapshot.callback_latency.p50_ms,
            snapshot.callback_latency.p95_ms,
            snapshot.callback_latency.p99_ms,
            snapshot.callback_latency.max_ms,
            snapshot.decision_superseded_total,
            snapshot.worker_active.get("decision", 0),
            snapshot.worker_limit.get("decision", 0),
            snapshot.queue_depth.get("decision", 0),
            snapshot.oldest_age_seconds.get("decision", 0.0),
            snapshot.dead_letters.get("decision", 0),
            agent_capacity.active,
            agent_capacity.limit,
            agent_capacity.waiting,
            agent_capacity.rejected_total,
            snapshot.agent_latency.count,
            snapshot.agent_latency.p50_ms,
            snapshot.agent_latency.p95_ms,
            snapshot.agent_latency.p99_ms,
            snapshot.agent_latency.max_ms,
            snapshot.callback_latency.count,
            snapshot.callback_latency.p50_ms,
            snapshot.callback_latency.p95_ms,
            snapshot.callback_latency.p99_ms,
            snapshot.callback_latency.max_ms,
            snapshot.agent_outcomes.get("success", 0),
            snapshot.agent_outcomes.get("timeout", 0),
            snapshot.agent_outcomes.get("overloaded", 0),
            snapshot.agent_outcomes.get("error", 0),
            snapshot.callback_outcomes.get("accepted", 0),
            snapshot.callback_outcomes.get("transport_error", 0),
            snapshot.callback_outcomes.get("protocol_error", 0),
            snapshot.callback_outcomes.get("rejected_lease_not_found", 0),
            snapshot.callback_outcomes.get("rejected_lease_expired", 0),
            snapshot.callback_outcomes.get("rejected_generation_mismatch", 0),
            snapshot.callback_outcomes.get("rejected_state_version_mismatch", 0),
            snapshot.callback_outcomes.get("rejected_session_not_running", 0),
            snapshot.callback_outcomes.get("rejected_invalid_payload", 0),
            snapshot.callback_outcomes.get("rejected_other", 0),
        )

    def _require_worker(self, worker_type: str) -> None:
        if worker_type not in self._worker_limit:
            raise ValueError(f"unknown worker type: {worker_type}")


class GatewayV2RuntimeMetricsReporter:
    def __init__(
        self,
        *,
        metrics: GatewayV2RuntimeMetrics,
        agent_capacity: AgentCapacityLimiter,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._metrics = metrics
        self._agent_capacity = agent_capacity
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="gateway-v2-runtime-metrics-reporter")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._metrics.log_snapshot(agent_capacity=self._agent_capacity.snapshot())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            self._metrics.log_snapshot(agent_capacity=self._agent_capacity.snapshot())
