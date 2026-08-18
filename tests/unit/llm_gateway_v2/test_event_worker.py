from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.event_worker import (
    ClaimedGatewayEvent,
    EventProcessResult,
    EventWorker,
    GenerationDisposition,
    classify_generation,
    event_claim_renewal_interval_seconds,
)
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry


def _claimed(
    *,
    session_id: str = "session-1",
    event_id: str = "event-1",
    generation: int = 1,
    sequence: int = 1,
    lock_ttl_ms: int = 30_000,
) -> ClaimedGatewayEvent:
    event_type = "session_started" if sequence == 1 else "session_stopped"
    payload: dict[str, Any]
    if sequence == 1:
        decision_lease_id = f"lease-{event_id}"
        payload = {
            "reason": "decision_requested",
            "lease": {
                "sessionId": session_id,
                "controlGeneration": generation,
                "decisionLeaseId": decision_lease_id,
                "stateVersion": 1,
                "leaseKind": "hosting_control",
                "allowedActions": ["wait"],
                "allowedSkillName": None,
                "allowedSkillNames": [],
                "parentSkillName": None,
            },
            "decisionContext": {
                "session": {"status": "active"},
                "availableSkills": [],
                "skillArgumentHints": [],
                "lastSkillResult": None,
            },
        }
    else:
        decision_lease_id = None
        payload = {
            "reason": "stopped",
            "stoppedAtMs": 1_700_000_000_000 + sequence,
        }
    event = parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": event_type,
            "sessionId": session_id,
            "controlGeneration": generation,
            "eventSequence": sequence,
            "stateVersion": 1,
            "decisionLeaseId": decision_lease_id,
            "occurredAtMs": 1_700_000_000_000 + sequence,
            "payload": payload,
        }
    )
    now = datetime.now(UTC)
    return ClaimedGatewayEvent(
        row_id=uuid4(),
        tenant_id=UUID("00000000-0000-0000-0000-000000000071"),
        cycle_id=uuid4(),
        gateway_id="gateway-1",
        session_id=session_id,
        event_id=event_id,
        event_type=event_type,
        control_generation=generation,
        event_sequence=sequence,
        event=event,
        content_hash="a" * 64,
        trace_id="trace-1",
        claim_token=uuid4(),
        claimed_fence_version=generation,
        attempt_count=1,
        locked_by="worker-1",
        lock_until=now + timedelta(milliseconds=lock_ttl_ms),
    )


@dataclass
class FakeRepository:
    claims: deque[ClaimedGatewayEvent | None]
    completions: list[tuple[ClaimedGatewayEvent, EventProcessResult]] = field(default_factory=list)
    sweep_result: int = 0
    dead_letter_count: int = 0
    completion_error: Exception | None = None
    completion_attempts: int = 0
    renew_result: bool = True
    renewals: int = 0
    count_error: Exception | None = None

    async def claim_next_event(
        self,
        *,
        worker_id: str,
        claim_ttl_ms: int,
        max_attempts: int,
    ) -> ClaimedGatewayEvent | None:
        del worker_id, claim_ttl_ms, max_attempts
        return self.claims.popleft() if self.claims else None

    async def complete_event(
        self,
        event: ClaimedGatewayEvent,
        result: EventProcessResult,
        *,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
    ) -> bool:
        del max_attempts, retry_base_ms, retry_max_ms
        self.completion_attempts += 1
        if self.completion_error is not None:
            raise self.completion_error
        self.completions.append((event, result))
        return True

    async def renew_event_claim(
        self,
        event: ClaimedGatewayEvent,
        *,
        claim_ttl_ms: int,
    ) -> bool:
        del event, claim_ttl_ms
        self.renewals += 1
        return self.renew_result

    async def sweep_expired_claims(self, *, max_attempts: int) -> int:
        del max_attempts
        return self.sweep_result

    async def count_dead_letters(self) -> int:
        if self.count_error is not None:
            raise self.count_error
        return self.dead_letter_count


def _worker(
    repository: FakeRepository,
    processor: Callable[[ClaimedGatewayEvent], Awaitable[EventProcessResult]],
    *,
    status: WorkerStatusRegistry | None = None,
    max_parallelism: int = 2,
    claim_ttl_ms: int = 30_000,
) -> EventWorker:
    return EventWorker(
        repository=repository,
        processor=processor,
        status_registry=status or WorkerStatusRegistry(),
        worker_id="worker-1",
        poll_interval_ms=5,
        claim_ttl_ms=claim_ttl_ms,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=max_parallelism,
    )


async def test_gap_blocks_later_sequence() -> None:
    repository = FakeRepository(deque([None]))
    processed: list[str] = []

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        processed.append(event.event_id)
        return EventProcessResult("succeeded")

    claimed_count = await _worker(repository, processor).run_once()

    assert claimed_count == 0
    assert processed == []
    assert repository.completions == []


async def test_worker_logs_event_processing_and_completion_elapsed_times(caplog) -> None:
    claim = _claimed()
    repository = FakeRepository(deque([claim, None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    with caplog.at_level(logging.INFO, logger="src.core.integration.llm_gateway_v2.event_worker"):
        await _worker(repository, processor, max_parallelism=1).run_once()

    processed = next(
        record
        for record in caplog.records
        if record.message == "LLM Gateway v2 event processing completed"
    )
    completed = next(
        record
        for record in caplog.records
        if record.message == "LLM Gateway v2 event completion committed"
    )
    assert processed.trace_id == claim.trace_id
    assert processed.event_id == claim.event_id
    assert processed.session_id == claim.session_id
    assert processed.control_generation == claim.control_generation
    assert processed.elapsed_ms >= 0
    assert completed.committed is True
    assert completed.elapsed_ms >= 0


async def test_different_sessions_can_run_concurrently() -> None:
    first = _claimed(session_id="session-1", event_id="event-1")
    second = _claimed(session_id="session-2", event_id="event-2")
    repository = FakeRepository(deque([first, second, None]))
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        nonlocal active, max_active
        del event
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        active -= 1
        return EventProcessResult("succeeded")

    run = asyncio.create_task(_worker(repository, processor).run_once())
    await asyncio.wait_for(both_started.wait(), timeout=2)
    release.set()

    assert await run == 2
    assert max_active == 2
    assert {item[0].session_id for item in repository.completions} == {"session-1", "session-2"}


@pytest.mark.parametrize(
    ("current", "incoming", "event_type", "sequence", "expected"),
    [
        (None, 1, "session_started", 1, GenerationDisposition.ACTIVATE_NEW),
        (1, 2, "session_started", 1, GenerationDisposition.ACTIVATE_NEW),
        (1, 2, "observation_updated", 2, GenerationDisposition.WAIT),
        (1, 2, "session_stopped", 2, GenerationDisposition.WAIT),
        (1, 1, "session_started", 1, GenerationDisposition.CURRENT),
        (1, 1, "observation_updated", 2, GenerationDisposition.CURRENT),
        (2, 1, "observation_updated", 2, GenerationDisposition.STALE),
        (2, 1, "session_stopped", 3, GenerationDisposition.STALE),
    ],
)
def test_generation_transition_matrix(
    current: int | None,
    incoming: int,
    event_type: str,
    sequence: int,
    expected: GenerationDisposition,
) -> None:
    assert classify_generation(current, incoming, event_type, sequence) is expected


def test_event_claim_renewal_checks_freshness_at_least_once_per_second() -> None:
    assert event_claim_renewal_interval_seconds(30_000) == 1.0
    assert event_claim_renewal_interval_seconds(150) == pytest.approx(0.05)


@pytest.mark.parametrize(
    "event_type",
    ["skill_started", "skill_finished", "decision_rejected"],
)
def test_old_generation_terminal_fact_events_enter_historical_recovery(
    event_type: str,
) -> None:
    assert classify_generation(2, 1, event_type, 3).value == "historical_recovery"


async def test_processor_crash_leaves_claim_for_expiry_recovery() -> None:
    claim = _claimed()
    repository = FakeRepository(deque([claim, None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        raise RuntimeError("processor crashed")

    assert await _worker(repository, processor).run_once() == 1
    assert repository.completions == []


async def test_completion_failure_is_not_retried_in_memory() -> None:
    claim = _claimed()
    repository = FakeRepository(deque([claim, None]), completion_error=RuntimeError("database unavailable"))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    assert await _worker(repository, processor).run_once() == 1
    assert repository.completions == []
    assert repository.completion_attempts == 1


async def test_long_processor_renews_claim_until_completion() -> None:
    repository = FakeRepository(deque([_claimed(lock_ttl_ms=150), None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        await asyncio.sleep(0.38)
        return EventProcessResult("succeeded")

    assert await _worker(repository, processor, claim_ttl_ms=150, max_parallelism=1).run_once() == 1
    assert repository.renewals >= 2
    assert len(repository.completions) == 1


async def test_long_processor_refreshes_worker_heartbeat_during_claim_renewal() -> None:
    ticks = iter(float(value) for value in range(1, 20))
    registry = WorkerStatusRegistry(monotonic=lambda: next(ticks))
    repository = FakeRepository(deque([_claimed(lock_ttl_ms=150), None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        await asyncio.sleep(0.18)
        return EventProcessResult("succeeded")

    await _worker(
        repository,
        processor,
        status=registry,
        claim_ttl_ms=150,
        max_parallelism=1,
    ).run_once()

    assert repository.renewals >= 2
    assert registry.snapshot().heartbeat_monotonic >= 5.0


async def test_long_processor_refreshes_heartbeat_before_first_claim_renewal() -> None:
    ticks = iter(float(value) for value in range(1, 30))
    registry = WorkerStatusRegistry(monotonic=lambda: next(ticks))
    repository = FakeRepository(deque([_claimed(), None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        await asyncio.sleep(0.04)
        return EventProcessResult("succeeded")

    await _worker(
        repository,
        processor,
        status=registry,
        claim_ttl_ms=300,
        max_parallelism=1,
    ).run_once()

    assert repository.renewals == 0
    assert registry.snapshot().heartbeat_monotonic > 2.0


async def test_heartbeat_continues_while_claim_renewal_is_pending() -> None:
    class BlockingRenewalRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__(deque([_claimed(lock_ttl_ms=150), None]))
            self.renewal_started = asyncio.Event()
            self.renewal_release = asyncio.Event()

        async def renew_event_claim(
            self,
            event: ClaimedGatewayEvent,
            *,
            claim_ttl_ms: int,
        ) -> bool:
            del event, claim_ttl_ms
            self.renewals += 1
            self.renewal_started.set()
            await self.renewal_release.wait()
            return True

    registry = WorkerStatusRegistry()
    repository = BlockingRenewalRepository()
    processor_release = asyncio.Event()

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        await processor_release.wait()
        return EventProcessResult("succeeded")

    running = asyncio.create_task(
        _worker(
            repository,
            processor,
            status=registry,
            claim_ttl_ms=150,
            max_parallelism=1,
        ).run_once()
    )
    try:
        await asyncio.wait_for(repository.renewal_started.wait(), timeout=1)
        heartbeat_before = registry.snapshot().heartbeat_monotonic
        await asyncio.sleep(0.02)
        heartbeat_after = registry.snapshot().heartbeat_monotonic
    finally:
        repository.renewal_release.set()
        processor_release.set()
        await asyncio.wait_for(running, timeout=1)

    assert heartbeat_before is not None
    assert heartbeat_after is not None
    assert heartbeat_after > heartbeat_before


async def test_claim_renewal_timeout_cancels_inflight_processor() -> None:
    class StalledRenewalRepository(FakeRepository):
        async def renew_event_claim(
            self,
            event: ClaimedGatewayEvent,
            *,
            claim_ttl_ms: int,
        ) -> bool:
            del event, claim_ttl_ms
            self.renewals += 1
            await asyncio.Event().wait()
            return True

    repository = StalledRenewalRepository(deque([_claimed(lock_ttl_ms=120), None]))
    processor_cancelled = asyncio.Event()

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        try:
            await asyncio.Event().wait()
        finally:
            processor_cancelled.set()

    worker = _worker(repository, processor, claim_ttl_ms=120, max_parallelism=1)

    assert await asyncio.wait_for(worker.run_once(), timeout=0.3) == 1
    assert repository.renewals == 1
    assert processor_cancelled.is_set()
    assert repository.completions == []


async def test_first_renewal_honors_database_lock_expiry_after_claim_delay() -> None:
    claim = _claimed(lock_ttl_ms=500)

    class DelayedPollRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__(deque([claim, None]))
            self.renewal_times: list[datetime] = []

        async def count_dead_letters(self) -> int:
            await asyncio.sleep(0.35)
            return 0

        async def renew_event_claim(
            self,
            event: ClaimedGatewayEvent,
            *,
            claim_ttl_ms: int,
        ) -> bool:
            del event, claim_ttl_ms
            self.renewals += 1
            self.renewal_times.append(datetime.now(UTC))
            return True

    repository = DelayedPollRepository()

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        await asyncio.sleep(0.1)
        return EventProcessResult("succeeded")

    assert await _worker(repository, processor, claim_ttl_ms=500, max_parallelism=1).run_once() == 1
    assert repository.renewal_times
    assert repository.renewal_times[0] < claim.lock_until


async def test_lost_claim_renewal_cancels_inflight_processor() -> None:
    repository = FakeRepository(deque([_claimed(), None]), renew_result=False)
    cancelled = asyncio.Event()

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    assert await _worker(repository, processor, claim_ttl_ms=30, max_parallelism=1).run_once() == 1
    assert cancelled.is_set()
    assert repository.completions == []


async def test_worker_status_reports_monotonic_heartbeat_and_degraded_count() -> None:
    clock_values = iter((10.0, 11.0, 12.0, 13.0, 14.0))
    registry = WorkerStatusRegistry(monotonic=lambda: next(clock_values))
    repository = FakeRepository(deque([None]), sweep_result=2, dead_letter_count=4)

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    worker = _worker(repository, processor, status=registry)
    await worker.run_once()
    snapshot = registry.snapshot()

    assert snapshot.state == "stopped"
    assert snapshot.heartbeat_monotonic == 11.0
    assert snapshot.last_successful_poll_monotonic == 12.0
    assert snapshot.dead_letter_count == 4
    assert snapshot.degraded is True


async def test_failed_dead_letter_count_does_not_mark_poll_successful() -> None:
    registry = WorkerStatusRegistry()
    repository = FakeRepository(deque([None]), count_error=RuntimeError("database unavailable"))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _worker(repository, processor, status=registry).run_once()

    assert registry.snapshot().last_successful_poll_monotonic is None


async def test_dead_letter_sweep_emits_stable_warning(caplog: pytest.LogCaptureFixture) -> None:
    repository = FakeRepository(deque([None]), sweep_result=2, dead_letter_count=2)

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    with caplog.at_level(logging.WARNING):
        await _worker(repository, processor).run_once()

    assert [record.getMessage() for record in caplog.records] == [
        "LLM Gateway v2 exhausted claims moved to dead letter"
    ]
    assert caplog.records[0].swept_count == 2


async def test_drain_stops_new_claims_and_waits_for_inflight_processor() -> None:
    claim = _claimed()
    repository = FakeRepository(deque([claim, None]))
    entered = asyncio.Event()
    release = asyncio.Event()
    registry = WorkerStatusRegistry()

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        return EventProcessResult("succeeded")

    worker = _worker(repository, processor, status=registry, max_parallelism=1)
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    draining = asyncio.create_task(worker.drain())
    await asyncio.sleep(0)
    assert registry.snapshot().state == "draining"
    assert not draining.done()

    release.set()
    await asyncio.wait_for(draining, timeout=2)
    assert registry.snapshot().state == "stopped"
    assert len(repository.completions) == 1


async def test_stop_cancels_the_loop_and_sets_stopped() -> None:
    repository = FakeRepository(deque([None]))
    registry = WorkerStatusRegistry()

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    worker = _worker(repository, processor, status=registry)
    await worker.start()
    assert registry.snapshot().state == "running"

    await worker.stop()

    assert registry.snapshot().state == "stopped"


async def test_worker_publishes_full_start_run_drain_stop_state_sequence() -> None:
    transitions: list[str] = []
    registry = WorkerStatusRegistry()
    registry.set_state_change_callback(lambda: transitions.append(registry.snapshot().state))
    repository = FakeRepository(deque([None]))

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    worker = _worker(repository, processor, status=registry)
    await worker.start()
    await worker.drain()

    assert transitions == ["starting", "running", "draining", "stopped"]


@pytest.mark.parametrize("poll_interval_ms", [0, -1])
def test_worker_rejects_non_positive_timing_configuration(poll_interval_ms: int) -> None:
    repository = FakeRepository(deque())

    async def processor(event: ClaimedGatewayEvent) -> EventProcessResult:
        del event
        return EventProcessResult("succeeded")

    with pytest.raises(ValueError, match="poll_interval_ms"):
        EventWorker(
            repository=repository,
            processor=processor,
            status_registry=WorkerStatusRegistry(),
            worker_id="worker-1",
            poll_interval_ms=poll_interval_ms,
            claim_ttl_ms=30_000,
            max_attempts=3,
            retry_base_ms=100,
            retry_max_ms=1_000,
            max_parallelism=1,
        )
