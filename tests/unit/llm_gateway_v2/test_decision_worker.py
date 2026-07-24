from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from src.core.integration.llm_gateway_v2.decision_client import (
    DecisionClientResult,
    DecisionClientTransportError,
)
from src.core.integration.llm_gateway_v2.decision_worker import DecisionWorker
from src.core.integration.llm_gateway_v2.outbox_repository import ClaimedDecision
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry


def _claim(*, token: str, attempt: int = 1) -> ClaimedDecision:
    return ClaimedDecision(
        row_id=UUID("00000000-0000-0000-0000-000000000101"),
        tenant_id=UUID("00000000-0000-0000-0000-000000000072"),
        cycle_id=UUID("00000000-0000-0000-0000-000000000102"),
        gateway_id="gateway-1",
        session_id="session-1",
        decision_id="decision-1",
        decision_lease_id="lease-1",
        control_generation=1,
        state_version=1,
        action="call_skill",
        request_body_bytes=b'{"stable":"body"}',
        body_hash="a" * 64,
        claim_token=UUID(token),
        claimed_fence_version=1,
        attempt_count=attempt,
        locked_by="worker-1",
        lock_until=datetime.now(UTC),
    )


class _Repository:
    def __init__(self, claims: list[ClaimedDecision]) -> None:
        self.claims = claims
        self.responses: list[tuple[ClaimedDecision, DecisionClientResult]] = []
        self.failures: list[tuple[ClaimedDecision, str, str]] = []
        self.renew_result = True
        self.swept = 0
        self.dead_letters = 0

    async def claim_next_decision(self, *, worker_id: str, claim_ttl_ms: int, max_attempts: int):
        del worker_id, claim_ttl_ms, max_attempts
        return self.claims.pop(0) if self.claims else None

    async def record_decision_response(self, decision, response):
        self.responses.append((decision, response))
        return True

    async def complete_decision_failure(
        self,
        decision,
        *,
        error_stage: str,
        error_category: str,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
    ):
        del max_attempts, retry_base_ms, retry_max_ms
        self.failures.append((decision, error_stage, error_category))
        return True

    async def renew_decision_claim(self, decision, *, claim_ttl_ms: int):
        del decision, claim_ttl_ms
        return self.renew_result

    async def sweep_expired_decision_claims(self, *, max_attempts: int):
        del max_attempts
        return self.swept

    async def count_decision_dead_letters(self):
        return self.dead_letters


class _Client:
    def __init__(self, outcomes: list[DecisionClientResult | Exception]) -> None:
        self.outcomes = outcomes
        self.raw_bodies: list[bytes] = []
        self.actions: list[str] = []

    async def send(self, *, action: str, raw_body: bytes) -> DecisionClientResult:
        self.actions.append(action)
        self.raw_bodies.append(raw_body)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _worker(repository: _Repository, client: _Client, *, claim_ttl_ms: int = 30_000) -> DecisionWorker:
    return DecisionWorker(
        repository=repository,
        client=client,
        status_registry=WorkerStatusRegistry(),
        worker_id="decision-worker",
        poll_interval_ms=10,
        claim_ttl_ms=claim_ttl_ms,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )


async def test_worker_records_body_first_accepted_response() -> None:
    claim = _claim(token="00000000-0000-0000-0000-000000000111")
    repository = _Repository([claim])
    response = DecisionClientResult(200, "accepted", "ok", "call-1")
    client = _Client([response])

    assert await _worker(repository, client).run_once() == 1

    assert repository.responses == [(claim, response)]
    assert repository.failures == []


async def test_worker_timeout_retry_reuses_exact_persisted_raw_bytes() -> None:
    first = _claim(token="00000000-0000-0000-0000-000000000111", attempt=1)
    second = replace(
        first,
        claim_token=UUID("00000000-0000-0000-0000-000000000112"),
        attempt_count=2,
    )
    repository = _Repository([first, second])
    response = DecisionClientResult(200, "accepted", "ok", "call-1")
    client = _Client([DecisionClientTransportError("timeout"), response])
    worker = _worker(repository, client)

    assert await worker.run_once() == 1
    assert await worker.run_once() == 1

    assert client.raw_bodies == [first.request_body_bytes, first.request_body_bytes]
    assert repository.failures == [(first, "http", "timeout")]
    assert repository.responses == [(second, response)]


async def test_worker_marks_idempotency_conflict_manual_through_repository() -> None:
    claim = _claim(token="00000000-0000-0000-0000-000000000111")
    repository = _Repository([claim])
    response = DecisionClientResult(409, "rejected", "idempotency_key_conflict", None)

    await _worker(repository, _Client([response])).run_once()

    assert repository.responses == [(claim, response)]
    assert response.is_idempotency_conflict is True


async def test_worker_lost_claim_cancels_in_flight_http() -> None:
    claim = _claim(token="00000000-0000-0000-0000-000000000111")
    repository = _Repository([claim])
    repository.renew_result = False
    cancelled = asyncio.Event()

    class _SlowClient:
        async def send(self, *, action: str, raw_body: bytes) -> DecisionClientResult:
            del action, raw_body
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    worker = DecisionWorker(
        repository=repository,
        client=_SlowClient(),
        status_registry=WorkerStatusRegistry(),
        worker_id="decision-worker",
        poll_interval_ms=10,
        claim_ttl_ms=3,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )

    assert await worker.run_once() == 1
    assert cancelled.is_set()
    assert repository.responses == []
    assert repository.failures == []


async def test_worker_updates_heartbeat_and_dead_letter_count_each_poll() -> None:
    repository = _Repository([])
    repository.swept = 2
    repository.dead_letters = 4
    status = WorkerStatusRegistry()
    worker = DecisionWorker(
        repository=repository,
        client=_Client([]),
        status_registry=status,
        worker_id="decision-worker",
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )

    assert await worker.run_once() == 0

    snapshot = status.snapshot()
    assert snapshot.heartbeat_monotonic is not None
    assert snapshot.last_successful_poll_monotonic is not None
    assert snapshot.dead_letter_count == 4


async def test_worker_publishes_full_start_run_drain_stop_state_sequence() -> None:
    transitions: list[str] = []
    status = WorkerStatusRegistry()
    status.set_state_change_callback(lambda: transitions.append(status.snapshot().state))
    worker = DecisionWorker(
        repository=_Repository([]),
        client=_Client([]),
        status_registry=status,
        worker_id="decision-worker",
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )

    await worker.start()
    await worker.drain()

    assert transitions == ["starting", "running", "draining", "stopped"]
