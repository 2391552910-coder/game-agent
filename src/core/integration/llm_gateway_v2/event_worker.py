from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from src.core.integration.llm_gateway_v2.contracts import GatewayV2Event
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields
from src.core.integration.llm_gateway_v2.runtime_metrics import GatewayV2RuntimeMetrics
from src.core.integration.llm_gateway_v2.worker_hooks import NO_OP_WORKER_HOOKS, WorkerHooks
from src.core.integration.llm_gateway_v2.worker_status import (
    WorkerStatusRegistry,
    WorkerStatusSnapshot,
)

logger = logging.getLogger(__name__)

ProcessOutcome = Literal["succeeded", "retryable_failed", "manual"]


def event_claim_renewal_interval_seconds(claim_ttl_ms: int) -> float:
    if claim_ttl_ms <= 0:
        raise ValueError("claim_ttl_ms must be positive")
    return min(max(claim_ttl_ms / 3_000, 0.001), 1.0)


class GenerationDisposition(StrEnum):
    ACTIVATE_NEW = "activate_new"
    CURRENT = "current"
    HISTORICAL_RECOVERY = "historical_recovery"
    STALE = "stale"
    WAIT = "wait"


def classify_generation(
    current_generation: int | None,
    incoming_generation: int,
    event_type: str,
    event_sequence: int,
) -> GenerationDisposition:
    if current_generation is None or incoming_generation > current_generation:
        if event_type == "session_started" and event_sequence == 1:
            return GenerationDisposition.ACTIVATE_NEW
        return GenerationDisposition.WAIT
    if incoming_generation == current_generation:
        return GenerationDisposition.CURRENT
    if event_type in {"skill_started", "skill_finished", "decision_rejected"}:
        return GenerationDisposition.HISTORICAL_RECOVERY
    return GenerationDisposition.STALE


@dataclass(frozen=True)
class ClaimedGatewayEvent:
    row_id: UUID
    tenant_id: UUID
    cycle_id: UUID
    gateway_id: str
    session_id: str
    event_id: str
    event_type: str
    control_generation: int
    event_sequence: int
    event: GatewayV2Event
    content_hash: str
    trace_id: str
    claim_token: UUID
    claimed_fence_version: int
    attempt_count: int
    locked_by: str
    lock_until: datetime
    historical_recovery: bool = False


@dataclass(frozen=True)
class EventProcessResult:
    outcome: ProcessOutcome
    error_stage: str | None = None
    error_category: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "retryable_failed", "manual"}:
            raise ValueError("unsupported event process outcome")


class EventProcessor(Protocol):
    async def __call__(self, event: ClaimedGatewayEvent) -> EventProcessResult: ...


class EventWorkRepository(Protocol):
    async def claim_next_event(
        self,
        *,
        worker_id: str,
        claim_ttl_ms: int,
        max_attempts: int,
    ) -> ClaimedGatewayEvent | None: ...

    async def complete_event(
        self,
        event: ClaimedGatewayEvent,
        result: EventProcessResult,
        *,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
    ) -> bool: ...

    async def renew_event_claim(
        self,
        event: ClaimedGatewayEvent,
        *,
        claim_ttl_ms: int,
    ) -> bool: ...

    async def sweep_expired_claims(self, *, max_attempts: int) -> int: ...

    async def count_dead_letters(self) -> int: ...


class EventWorker:
    def __init__(
        self,
        *,
        repository: EventWorkRepository,
        processor: EventProcessor | Callable[[ClaimedGatewayEvent], Awaitable[EventProcessResult]],
        status_registry: WorkerStatusRegistry,
        worker_id: str,
        poll_interval_ms: int,
        claim_ttl_ms: int,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
        max_parallelism: int,
        hooks: WorkerHooks = NO_OP_WORKER_HOOKS,
        metrics: GatewayV2RuntimeMetrics | None = None,
    ) -> None:
        self._validate_configuration(
            worker_id=worker_id,
            poll_interval_ms=poll_interval_ms,
            claim_ttl_ms=claim_ttl_ms,
            max_attempts=max_attempts,
            retry_base_ms=retry_base_ms,
            retry_max_ms=retry_max_ms,
            max_parallelism=max_parallelism,
        )
        self._repository = repository
        self._processor = processor
        self._status = status_registry
        self._worker_id = worker_id
        self._poll_interval_seconds = poll_interval_ms / 1_000
        self._claim_ttl_ms = claim_ttl_ms
        self._max_attempts = max_attempts
        self._retry_base_ms = retry_base_ms
        self._retry_max_ms = retry_max_ms
        self._max_parallelism = max_parallelism
        self._hooks = hooks
        self._metrics = metrics
        self._stop_requested = asyncio.Event()
        self._drain_requested = asyncio.Event()
        self._started = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._heartbeat_interval_seconds = min(max(self._poll_interval_seconds / 2, 0.05), 0.5)
        self._maintenance_interval_seconds = max(self._poll_interval_seconds * 10, 1.0)

    @staticmethod
    def _validate_configuration(
        *,
        worker_id: str,
        poll_interval_ms: int,
        claim_ttl_ms: int,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
        max_parallelism: int,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        for name, value in (
            ("poll_interval_ms", poll_interval_ms),
            ("claim_ttl_ms", claim_ttl_ms),
            ("max_attempts", max_attempts),
            ("retry_base_ms", retry_base_ms),
            ("retry_max_ms", retry_max_ms),
            ("max_parallelism", max_parallelism),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if retry_base_ms > retry_max_ms:
            raise ValueError("retry_base_ms must not exceed retry_max_ms")

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop_requested.clear()
        self._drain_requested.clear()
        self._started.clear()
        self._status.mark_starting()
        self._loop_task = asyncio.create_task(self._run_loop(), name=f"llm-gateway-v2-{self._worker_id}")
        await self._started.wait()

    def status_snapshot(self) -> WorkerStatusSnapshot:
        return self._status.snapshot()

    async def stop(self) -> None:
        task = self._loop_task
        if task is None:
            self._status.mark_stopped()
            return
        self._stop_requested.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._loop_task = None
            self._status.mark_stopped()

    async def drain(self) -> None:
        task = self._loop_task
        if task is None:
            self._status.mark_stopped()
            return
        self._status.mark_draining()
        self._drain_requested.set()
        await task
        self._loop_task = None
        self._status.mark_stopped()

    async def run_once(self, *, include_maintenance: bool = True) -> int:
        self._status.heartbeat()
        self._status.mark_poll_started()
        if include_maintenance:
            await self._run_maintenance_once()

        active_tasks: set[asyncio.Task[None]] = set()
        claimed_count = 0
        try:
            while True:
                completed = {task for task in active_tasks if task.done()}
                if completed:
                    await asyncio.gather(*completed)
                    active_tasks.difference_update(completed)

                while (
                    len(active_tasks) < self._max_parallelism
                    and not self._drain_requested.is_set()
                    and not self._stop_requested.is_set()
                ):
                    event = await self._repository.claim_next_event(
                        worker_id=self._worker_id,
                        claim_ttl_ms=self._claim_ttl_ms,
                        max_attempts=self._max_attempts,
                    )
                    if event is None:
                        break
                    active_tasks.add(
                        asyncio.create_task(
                            self._process_one(event),
                            name=f"llm-gateway-v2-process-slot-{event.event_id}",
                        )
                    )
                    claimed_count += 1
                    self._status.mark_progress()

                    completed = {task for task in active_tasks if task.done()}
                    if completed:
                        await asyncio.gather(*completed)
                        active_tasks.difference_update(completed)

                self._status.heartbeat()
                self._status.mark_successful_poll()
                if not active_tasks:
                    break

                completed, _ = await asyncio.wait(
                    active_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await asyncio.gather(*completed)
                active_tasks.difference_update(completed)
        finally:
            await self._cancel_and_wait(*active_tasks)
            self._status.mark_poll_completed()
        return claimed_count

    async def _run_maintenance_once(self) -> None:
        swept_count = await self._repository.sweep_expired_claims(max_attempts=self._max_attempts)
        if swept_count:
            logger.warning(
                "LLM Gateway v2 exhausted claims moved to dead letter",
                extra={"swept_count": swept_count},
            )
        dead_letter_count = await self._repository.count_dead_letters()
        self._status.set_dead_letter_count(dead_letter_count)
        if self._metrics is not None:
            self._metrics.set_dead_letters("event", dead_letter_count)
            await self._refresh_queue_metrics()

    async def _process_one(self, event: ClaimedGatewayEvent) -> None:
        if self._metrics is not None:
            self._metrics.task_started("event")
        try:
            await self._process_claimed_event(event)
        finally:
            if self._metrics is not None:
                self._metrics.task_finished("event")
            self._status.mark_progress()

    async def _process_claimed_event(self, event: ClaimedGatewayEvent) -> None:
        processing_started = time.monotonic()
        processor_task: asyncio.Task[EventProcessResult] = asyncio.create_task(
            self._invoke_processor(event),
            name=f"llm-gateway-v2-process-{event.event_id}",
        )
        renewal_task = asyncio.create_task(
            self._maintain_claim(event),
            name=f"llm-gateway-v2-renew-{event.event_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {processor_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                try:
                    renewal_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error(
                        "LLM Gateway v2 event claim renewal failed",
                        extra={
                            **safe_exception_fields(
                                stage="worker",
                                category="claim_renewal_failed",
                                error=error,
                                trace_id=event.trace_id,
                                event_id=event.event_id,
                                session_id=event.session_id,
                                control_generation=event.control_generation,
                                elapsed_ms=(time.monotonic() - processing_started) * 1_000,
                            ),
                            "worker_id": self._worker_id,
                        },
                    )
                else:
                    logger.warning(
                        "LLM Gateway v2 event claim was lost",
                        extra={
                            "trace_id": event.trace_id,
                            "event_id": event.event_id,
                            "session_id": event.session_id,
                            "control_generation": event.control_generation,
                            "elapsed_ms": (time.monotonic() - processing_started) * 1_000,
                            "worker_id": self._worker_id,
                        },
                    )
                await self._cancel_and_wait(processor_task)
                return

            await self._cancel_and_wait(renewal_task)
            result = processor_task.result()
        except asyncio.CancelledError:
            await self._cancel_and_wait(processor_task, renewal_task)
            raise
        except Exception as error:
            logger.error(
                "LLM Gateway v2 event processor stopped before durable completion",
                extra={
                    **safe_exception_fields(
                        stage="event",
                        category="processor_failed",
                        error=error,
                        trace_id=event.trace_id,
                        event_id=event.event_id,
                        session_id=event.session_id,
                        control_generation=event.control_generation,
                        elapsed_ms=(time.monotonic() - processing_started) * 1_000,
                    ),
                    "worker_id": self._worker_id,
                },
            )
            return

        processor_elapsed_ms = (time.monotonic() - processing_started) * 1_000
        logger.info(
            "LLM Gateway v2 event processing completed",
            extra={
                "trace_id": event.trace_id,
                "event_id": event.event_id,
                "session_id": event.session_id,
                "control_generation": event.control_generation,
                "event_type": event.event_type,
                "outcome": result.outcome,
                "error_stage": result.error_stage,
                "error_category": result.error_category,
                "elapsed_ms": processor_elapsed_ms,
                "worker_id": self._worker_id,
            },
        )

        completion_started = time.monotonic()
        try:
            committed = await self._repository.complete_event(
                event,
                result,
                max_attempts=self._max_attempts,
                retry_base_ms=self._retry_base_ms,
                retry_max_ms=self._retry_max_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "LLM Gateway v2 event completion was not committed",
                extra={
                    **safe_exception_fields(
                        stage="database",
                        category="event_completion_failed",
                        error=error,
                        trace_id=event.trace_id,
                        event_id=event.event_id,
                        session_id=event.session_id,
                        control_generation=event.control_generation,
                        elapsed_ms=(time.monotonic() - completion_started) * 1_000,
                    ),
                    "worker_id": self._worker_id,
                },
            )
        else:
            logger.info(
                "LLM Gateway v2 event completion committed",
                extra={
                    "trace_id": event.trace_id,
                    "event_id": event.event_id,
                    "session_id": event.session_id,
                    "control_generation": event.control_generation,
                    "outcome": result.outcome,
                    "committed": committed,
                    "elapsed_ms": (time.monotonic() - completion_started) * 1_000,
                    "worker_id": self._worker_id,
                },
            )

    async def _refresh_queue_metrics(self) -> None:
        queue_reader = getattr(self._repository, "queue_metrics", None)
        if not callable(queue_reader):
            return
        try:
            queue = await queue_reader(max_attempts=self._max_attempts)
        except Exception as error:
            logger.warning(
                "LLM Gateway v2 event queue metrics refresh failed",
                extra=safe_exception_fields(
                    stage="metrics",
                    category="queue_metrics_failed",
                    error=error,
                ),
            )
            return
        self._metrics.set_queue("event", queue)

    async def _invoke_processor(self, event: ClaimedGatewayEvent) -> EventProcessResult:
        await self._hooks.before_agent(event.event_id)
        return await self._processor(event)

    async def _maintain_claim(self, event: ClaimedGatewayEvent) -> None:
        claim_ttl_seconds = max(self._claim_ttl_ms / 1_000, 0.001)
        renewal_interval_seconds = event_claim_renewal_interval_seconds(self._claim_ttl_ms)
        heartbeat_interval_seconds = min(
            renewal_interval_seconds,
            max(self._poll_interval_seconds, 0.001),
            1.0,
        )
        loop = asyncio.get_running_loop()
        remaining_claim_seconds = max(
            (event.lock_until - datetime.now(tz=event.lock_until.tzinfo)).total_seconds(),
            0.0,
        )
        claim_expires_at = loop.time() + remaining_claim_seconds
        next_renewal = loop.time() + min(
            renewal_interval_seconds,
            max(remaining_claim_seconds / 3, 0.001),
        )
        while True:
            await asyncio.sleep(
                min(
                    heartbeat_interval_seconds,
                    max(next_renewal - loop.time(), 0.0),
                )
            )
            self._status.heartbeat()
            if loop.time() < next_renewal:
                continue
            renewed = await self._renew_claim_before_expiry(
                event,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                timeout_seconds=max(claim_expires_at - loop.time(), 0.0),
            )
            if not renewed:
                return
            self._status.heartbeat()
            renewed_at = loop.time()
            next_renewal = renewed_at + renewal_interval_seconds
            claim_expires_at = renewed_at + claim_ttl_seconds

    async def _renew_claim_before_expiry(
        self,
        event: ClaimedGatewayEvent,
        *,
        heartbeat_interval_seconds: float,
        timeout_seconds: float,
    ) -> bool:
        if timeout_seconds <= 0:
            return False
        renewal_task = asyncio.create_task(
            self._repository.renew_event_claim(
                event,
                claim_ttl_ms=self._claim_ttl_ms,
            ),
            name=f"llm-gateway-v2-renew-request-{event.event_id}",
        )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            while True:
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if remaining_seconds <= 0:
                    return False
                done, _ = await asyncio.wait(
                    {renewal_task},
                    timeout=min(heartbeat_interval_seconds, remaining_seconds),
                )
                self._status.heartbeat()
                if renewal_task in done:
                    return renewal_task.result()
        finally:
            if not renewal_task.done():
                await self._cancel_and_wait(renewal_task)

    @staticmethod
    async def _cancel_and_wait(*tasks: asyncio.Task[Any]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_loop(self) -> None:
        self._status.mark_running()
        self._status.heartbeat()
        self._watchdog_task = asyncio.create_task(
            self._heartbeat_watchdog(),
            name=f"llm-gateway-v2-heartbeat-{self._worker_id}",
        )
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name=f"llm-gateway-v2-maintenance-{self._worker_id}",
        )
        self._started.set()
        try:
            while not self._stop_requested.is_set():
                try:
                    await self.run_once(include_maintenance=False)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._status.heartbeat()
                    logger.error(
                        "LLM Gateway v2 worker poll failed",
                        extra={
                            **safe_exception_fields(
                                stage="worker",
                                category="poll_failed",
                                error=error,
                            ),
                            "worker_id": self._worker_id,
                        },
                    )
                if self._drain_requested.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_requested.wait(), timeout=self._poll_interval_seconds)
                except TimeoutError:
                    continue
        finally:
            await self._cancel_background_tasks()
            self._started.set()
            self._status.mark_stopped()

    async def _heartbeat_watchdog(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stop_requested.wait(),
                    timeout=self._heartbeat_interval_seconds,
                )
            except TimeoutError:
                self._status.heartbeat()
            else:
                return

    async def _maintenance_loop(self) -> None:
        first_run = True
        while True:
            if first_run:
                first_run = False
            else:
                try:
                    await asyncio.wait_for(
                        self._stop_requested.wait(),
                        timeout=self._maintenance_interval_seconds,
                    )
                except TimeoutError:
                    pass
                else:
                    return
            try:
                await self._run_maintenance_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._status.mark_database_error()
                logger.warning(
                    "LLM Gateway v2 event maintenance failed",
                    extra=safe_exception_fields(
                        stage="database",
                        category="maintenance_failed",
                        error=error,
                    ),
                )

    async def _cancel_background_tasks(self) -> None:
        tasks = tuple(task for task in (self._watchdog_task, self._maintenance_task) if task is not None)
        self._watchdog_task = None
        self._maintenance_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
