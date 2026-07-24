from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from src.core.integration.llm_gateway_v2.decision_client import (
    DecisionClientProtocolError,
    DecisionClientResult,
    DecisionClientTransportError,
)
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields
from src.core.integration.llm_gateway_v2.outbox_repository import ClaimedDecision
from src.core.integration.llm_gateway_v2.worker_hooks import NO_OP_WORKER_HOOKS, WorkerHooks
from src.core.integration.llm_gateway_v2.worker_status import (
    WorkerStatusRegistry,
    WorkerStatusSnapshot,
)

logger = logging.getLogger(__name__)


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
        self._status.mark_successful_poll()
        if claimed:
            await asyncio.gather(*(self._process_one(decision) for decision in claimed))
        return len(claimed)

    async def _process_one(self, decision: ClaimedDecision) -> None:
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
                                decision_id=decision.decision_id,
                            ),
                            "worker_id": self._worker_id,
                        },
                    )
                else:
                    logger.warning(
                        "LLM Gateway v2 decision claim was lost",
                        extra={"decision_id": decision.decision_id, "worker_id": self._worker_id},
                    )
                await self._cancel_and_wait(request_task)
                return

            await self._cancel_and_wait(renewal_task)
            try:
                response = request_task.result()
            except DecisionClientTransportError as error:
                await self._complete_failure(decision, error.category)
                return
            except DecisionClientProtocolError as error:
                await self._complete_failure(decision, error.category)
                return
        except asyncio.CancelledError:
            await self._cancel_and_wait(request_task, renewal_task)
            raise
        except Exception as error:
            logger.error(
                "LLM Gateway v2 decision HTTP stopped before durable completion",
                extra={
                    **safe_exception_fields(
                        stage="http",
                        category="request_stopped",
                        error=error,
                        decision_id=decision.decision_id,
                    ),
                    "worker_id": self._worker_id,
                },
            )
            return

        try:
            await self._repository.record_decision_response(decision, response)
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
                        decision_id=decision.decision_id,
                    ),
                    "worker_id": self._worker_id,
                },
            )

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
                        decision_id=decision.decision_id,
                    ),
                    "worker_id": self._worker_id,
                },
            )

    async def _maintain_claim(self, decision: ClaimedDecision) -> None:
        renewal_interval_seconds = max(self._claim_ttl_ms / 3_000, 0.001)
        while True:
            await asyncio.sleep(renewal_interval_seconds)
            renewed = await self._repository.renew_decision_claim(
                decision,
                claim_ttl_ms=self._claim_ttl_ms,
            )
            if not renewed:
                return

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
