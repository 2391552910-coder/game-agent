from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from src.core.integration.llm_gateway_v2.decision_client import (
    DecisionClientProtocolError,
    DecisionClientResult,
    DecisionClientTransportError,
)
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields
from src.core.integration.llm_gateway_v2.outbox_repository import ClaimedDecision
from src.core.integration.llm_gateway_v2.runtime_metrics import GatewayV2RuntimeMetrics
from src.core.integration.llm_gateway_v2.worker_hooks import NO_OP_WORKER_HOOKS, WorkerHooks
from src.core.integration.llm_gateway_v2.worker_status import (
    WorkerStatusRegistry,
    WorkerStatusSnapshot,
)

logger = logging.getLogger(__name__)

_CALLBACK_REJECTION_REASONS: tuple[str, ...] = (
    "lease_not_found",
    "lease_expired",
    "generation_mismatch",
    "state_version_mismatch",
    "session_not_running",
    "invalid_payload",
)


def callback_metric_outcome(response: DecisionClientResult) -> str:
    if response.status != "rejected":
        return response.status
    reason = response.reason.strip().casefold().replace("-", "_").replace(" ", "_")
    if "stale_control_generation" in reason or "generation_mismatch" in reason:
        normalized = "generation_mismatch"
    else:
        normalized = next(
            (candidate for candidate in _CALLBACK_REJECTION_REASONS if candidate in reason),
            "other",
        )
    return f"rejected_{normalized}"


def _decision_identity_fields(
    decision: ClaimedDecision,
    response: DecisionClientResult | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": (response.trace_id if response is not None else None) or decision.trace_id,
        "session_id": (response.session_id if response is not None else None) or decision.session_id,
        "decision_id": (response.decision_id if response is not None else None) or decision.decision_id,
        "decision_lease_id": (response.decision_lease_id if response is not None else None)
        or decision.decision_lease_id,
        "control_generation": (
            response.control_generation if response is not None else None
        )
        or decision.control_generation,
        "state_version": (response.state_version if response is not None else None) or decision.state_version,
        "skill_call_id": response.skill_call_id if response is not None else None,
    }


class DecisionWorkRepository(Protocol):
    async def claim_next_decision(
        self,
        *,
        worker_id: str,
        claim_ttl_ms: int,
        max_attempts: int,
    ) -> ClaimedDecision | None: ...

    async def record_decision_response(
        self,
        decision: ClaimedDecision,
        response: DecisionClientResult,
    ) -> bool: ...

    async def complete_decision_failure(
        self,
        decision: ClaimedDecision,
        *,
        error_stage: str,
        error_category: str,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
    ) -> bool: ...

    async def renew_decision_claim(
        self,
        decision: ClaimedDecision,
        *,
        claim_ttl_ms: int,
    ) -> bool: ...

    async def sweep_expired_decision_claims(self, *, max_attempts: int) -> int: ...

    async def count_decision_dead_letters(self) -> int: ...


class DecisionHttpClient(Protocol):
    async def send(self, *, action: str, raw_body: bytes) -> DecisionClientResult: ...


class DecisionWorker:
    def __init__(
        self,
        *,
        repository: DecisionWorkRepository,
        client: DecisionHttpClient,
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
        self._client = client
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
        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name=f"llm-gateway-v2-{self._worker_id}",
        )
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

    async def run_once(self) -> int:
        self._status.heartbeat()
        swept_count = await self._repository.sweep_expired_decision_claims(
            max_attempts=self._max_attempts,
        )
        if swept_count:
            logger.warning(
                "LLM Gateway v2 exhausted decision claims moved to dead letter",
                extra={"swept_count": swept_count},
            )

        claimed: list[ClaimedDecision] = []
        for _ in range(self._max_parallelism):
            if self._drain_requested.is_set() or self._stop_requested.is_set():
                break
            decision = await self._repository.claim_next_decision(
                worker_id=self._worker_id,
                claim_ttl_ms=self._claim_ttl_ms,
                max_attempts=self._max_attempts,
            )
            if decision is None:
                break
            claimed.append(decision)

        self._status.heartbeat()
        dead_letter_count = await self._repository.count_decision_dead_letters()
        self._status.set_dead_letter_count(dead_letter_count)
        if self._metrics is not None:
            self._metrics.set_dead_letters("decision", dead_letter_count)
            await self._refresh_queue_metrics()
        self._status.mark_successful_poll()
        if claimed:
            await asyncio.gather(*(self._process_one(decision) for decision in claimed))
        return len(claimed)

    async def _process_one(self, decision: ClaimedDecision) -> None:
        if self._metrics is not None:
            self._metrics.task_started("decision")
        try:
            await self._process_claimed_decision(decision)
        except asyncio.CancelledError:
            raise
        finally:
            if self._metrics is not None:
                self._metrics.task_finished("decision")

    async def _process_claimed_decision(self, decision: ClaimedDecision) -> str:
        http_started = time.monotonic()
        request_task = asyncio.create_task(
            self._send_decision(decision),
            name=f"llm-gateway-v2-send-{decision.decision_id}",
        )
        renewal_task = asyncio.create_task(
            self._maintain_claim(decision),
            name=f"llm-gateway-v2-renew-decision-{decision.decision_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {request_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                try:
                    renewal_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error(
                        "LLM Gateway v2 decision claim renewal failed",
                        extra={
                            **safe_exception_fields(
                                stage="worker",
                                category="claim_renewal_failed",
                                error=error,
                                **_decision_identity_fields(decision),
                                elapsed_ms=(time.monotonic() - http_started) * 1_000,
                            ),
                            "worker_id": self._worker_id,
                        },
                    )
                else:
                    logger.warning(
                        "LLM Gateway v2 decision claim was lost",
                        extra={
                            **_decision_identity_fields(decision),
                            "worker_id": self._worker_id,
                            "elapsed_ms": (time.monotonic() - http_started) * 1_000,
                        },
                    )
                await self._cancel_and_wait(request_task)
                if self._metrics is not None:
                    self._metrics.record_callback_result(
                        "claim_lost",
                        elapsed_ms=(time.monotonic() - http_started) * 1_000,
                    )
                return "claim_lost"

            await self._cancel_and_wait(renewal_task)
            try:
                response = request_task.result()
            except DecisionClientTransportError as error:
                logger.warning(
                    "LLM Gateway v2 decision HTTP failed",
                    extra={
                        **_decision_identity_fields(decision),
                        "http_status": None,
                        "response_status": None,
                        "response_reason": None,
                        "error_category": error.category,
                        "elapsed_ms": (time.monotonic() - http_started) * 1_000,
                        "worker_id": self._worker_id,
                    },
                )
                await self._complete_failure(decision, error.category)
                if self._metrics is not None:
                    self._metrics.record_callback_result(
                        "transport_error",
                        elapsed_ms=(time.monotonic() - http_started) * 1_000,
                    )
                return "transport_error"
            except DecisionClientProtocolError as error:
                logger.warning(
                    "LLM Gateway v2 decision HTTP failed",
                    extra={
                        **_decision_identity_fields(decision),
                        "http_status": error.http_status,
                        "response_status": None,
                        "response_reason": None,
                        "error_category": error.category,
                        "elapsed_ms": (time.monotonic() - http_started) * 1_000,
                        "worker_id": self._worker_id,
                    },
                )
                await self._complete_failure(decision, error.category)
                if self._metrics is not None:
                    self._metrics.record_callback_result(
                        "protocol_error",
                        elapsed_ms=(time.monotonic() - http_started) * 1_000,
                    )
                return "protocol_error"
        except asyncio.CancelledError:
            await self._cancel_and_wait(request_task, renewal_task)
            if self._metrics is not None:
                self._metrics.record_callback_result(
                    "cancelled",
                    elapsed_ms=(time.monotonic() - http_started) * 1_000,
                )
            raise
        except Exception as error:
            logger.error(
                "LLM Gateway v2 decision HTTP stopped before durable completion",
                extra={
                    **safe_exception_fields(
                        stage="http",
                        category="request_stopped",
                        error=error,
                        **_decision_identity_fields(decision),
                        elapsed_ms=(time.monotonic() - http_started) * 1_000,
                    ),
                    "worker_id": self._worker_id,
                },
            )
            if self._metrics is not None:
                self._metrics.record_callback_result(
                    "error",
                    elapsed_ms=(time.monotonic() - http_started) * 1_000,
                )
            return "error"

        logger.info(
            "LLM Gateway v2 decision HTTP completed",
            extra={
                **_decision_identity_fields(decision, response),
                "http_status": response.http_status,
                "response_status": response.status,
                "response_reason": response.reason[:256],
                "elapsed_ms": (time.monotonic() - http_started) * 1_000,
                "worker_id": self._worker_id,
            },
        )
        if self._metrics is not None:
            self._metrics.record_callback_result(
                callback_metric_outcome(response),
                elapsed_ms=(time.monotonic() - http_started) * 1_000,
            )

        commit_started = time.monotonic()
        try:
            committed = await self._repository.record_decision_response(decision, response)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "LLM Gateway v2 decision response was not committed",
                extra={
                    **safe_exception_fields(
                        stage="database",
                        category="response_commit_failed",
                        error=error,
                        **_decision_identity_fields(decision, response),
                        http_status=response.http_status,
                        response_status=response.status,
                        response_reason=response.reason,
                        elapsed_ms=(time.monotonic() - commit_started) * 1_000,
                    ),
                    "worker_id": self._worker_id,
                },
            )
        else:
            logger.info(
                "LLM Gateway v2 decision response commit completed",
                extra={
                    **_decision_identity_fields(decision, response),
                    "http_status": response.http_status,
                    "response_status": response.status,
                    "response_reason": response.reason[:256],
                    "committed": committed,
                    "elapsed_ms": (time.monotonic() - commit_started) * 1_000,
                    "worker_id": self._worker_id,
                },
            )
        return callback_metric_outcome(response)

    async def _refresh_queue_metrics(self) -> None:
        queue_reader = getattr(self._repository, "queue_metrics", None)
        if not callable(queue_reader):
            return
        try:
            queue = await queue_reader()
        except Exception as error:
            logger.warning(
                "LLM Gateway v2 decision queue metrics refresh failed",
                extra=safe_exception_fields(
                    stage="metrics",
                    category="queue_metrics_failed",
                    error=error,
                ),
            )
            return
        self._metrics.set_queue("decision", queue)

    async def _send_decision(self, decision: ClaimedDecision) -> DecisionClientResult:
        await self._hooks.before_decision_http(decision.decision_id)
        response = await self._client.send(
            action=decision.action,
            raw_body=decision.request_body_bytes,
        )
        await self._hooks.after_decision_http(decision.decision_id)
        return response

    async def _complete_failure(self, decision: ClaimedDecision, category: str) -> None:
        try:
            await self._repository.complete_decision_failure(
                decision,
                error_stage="http",
                error_category=category,
                max_attempts=self._max_attempts,
                retry_base_ms=self._retry_base_ms,
                retry_max_ms=self._retry_max_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "LLM Gateway v2 decision failure was not committed",
                extra={
                    **safe_exception_fields(
                        stage="database",
                        category="failure_commit_failed",
                        error=error,
                        **_decision_identity_fields(decision),
                    ),
                    "worker_id": self._worker_id,
                },
            )

    async def _maintain_claim(self, decision: ClaimedDecision) -> None:
        renewal_interval_seconds = max(self._claim_ttl_ms / 3_000, 0.001)
        heartbeat_interval_seconds = min(
            renewal_interval_seconds,
            max(self._poll_interval_seconds, 0.001),
            1.0,
        )
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + renewal_interval_seconds
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
            renewed = await self._renew_decision_claim_with_heartbeat(
                decision,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                timeout_seconds=renewal_interval_seconds,
            )
            if not renewed:
                return
            self._status.heartbeat()
            next_renewal = loop.time() + renewal_interval_seconds

    async def _renew_decision_claim_with_heartbeat(
        self,
        decision: ClaimedDecision,
        *,
        heartbeat_interval_seconds: float,
        timeout_seconds: float,
    ) -> bool:
        if timeout_seconds <= 0:
            return False
        renewal_task = asyncio.create_task(
            self._repository.renew_decision_claim(
                decision,
                claim_ttl_ms=self._claim_ttl_ms,
            ),
            name=f"llm-gateway-v2-renew-decision-request-{decision.decision_id}",
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
        self._started.set()
        try:
            while not self._stop_requested.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._status.heartbeat()
                    logger.error(
                        "LLM Gateway v2 decision worker poll failed",
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
                    await asyncio.wait_for(
                        self._stop_requested.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
        finally:
            self._started.set()
            self._status.mark_stopped()
