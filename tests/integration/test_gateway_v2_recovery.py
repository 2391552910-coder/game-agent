from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from src.core.agents.gateway_v2_models import (
    GatewayV2CallSkillAction,
    GatewayV2NoOpAction,
    GatewayV2WaitAction,
)
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2BatchEnvelope,
    GatewayV2Event,
    parse_gateway_v2_event,
)
from src.core.integration.llm_gateway_v2.decision_client import DecisionClientResult
from src.core.integration.llm_gateway_v2.decision_service import build_gateway_v2_agent_context
from src.core.integration.llm_gateway_v2.decision_worker import DecisionWorker
from src.core.integration.llm_gateway_v2.event_service import EventService
from src.core.integration.llm_gateway_v2.event_worker import EventProcessResult, EventWorker
from src.core.integration.llm_gateway_v2.inbox_repository import InboxRepository
from src.core.integration.llm_gateway_v2.outbox_repository import (
    DecisionPlanConflictError,
    DecisionPlanFencedError,
    DecisionPlanUnavailableError,
    OutboxRepository,
)
from src.core.integration.llm_gateway_v2.terminal_repository import MutationDisposition, TerminalRepository
from src.core.integration.llm_gateway_v2.worker_hooks import NoOpWorkerHooks
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry

pytestmark = pytest.mark.asyncio

TENANT_ID = UUID("00000000-0000-0000-0000-000000000072")
IDENTITY = InboundGatewayIdentity("gateway-events", "gateway-recovery", TENANT_ID)


def _event(
    event_id: str,
    *,
    session_id: str = "session-1",
    generation: int = 1,
    sequence: int = 1,
    event_type: str | None = None,
    stop_reason: str = "hosting stopped",
    decision_lease_id: str | None = None,
    state_version: int | None = None,
) -> GatewayV2Event:
    resolved_type = event_type or ("session_started" if sequence == 1 else "observation_updated")
    if resolved_type in {"session_started", "observation_updated"}:
        payload = {
            "lease": {
                "decisionLeaseId": decision_lease_id or f"lease-{session_id}-{generation}-{sequence}",
                "stateVersion": sequence if state_version is None else state_version,
                "leaseKind": "hosting_control",
                "allowedDecisionActions": ["wait"],
                "session": {"status": "active", "generation": generation},
                "availableSkills": [],
                "skillArgumentHints": [],
            }
        }
    elif resolved_type == "session_stopped":
        payload = {"reason": stop_reason}
    else:
        raise AssertionError(f"unsupported test event type: {resolved_type}")
    return parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": resolved_type,
            "sessionId": session_id,
            "controlGeneration": generation,
            "eventSequence": sequence,
            "occurredAtMs": 1_700_000_000_000 + generation * 100 + sequence,
            "payload": payload,
        }
    )


@pytest.fixture(scope="module", autouse=True)
def _upgrade_schema(migration_config) -> None:
    command.upgrade(migration_config, "009")


@pytest.fixture
async def session_factory(verified_test_postgres_url: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(verified_test_postgres_url, poolclass=sa.pool.NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with engine.begin() as connection:
        await connection.execute(sa.text("DELETE FROM llm_gateway_skill_calls"))
        await connection.execute(sa.text("DELETE FROM llm_gateway_decisions"))
        await connection.execute(sa.text("DELETE FROM llm_gateway_events"))
        await connection.execute(sa.text("DELETE FROM llm_gateway_control_cycles"))
        await connection.execute(sa.text("DELETE FROM llm_gateway_sessions"))
        await connection.execute(
            sa.text("DELETE FROM action_tracking WHERE tenant_id = :tenant_id"), {"tenant_id": TENANT_ID}
        )
        await connection.execute(sa.text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": TENANT_ID})
        await connection.execute(
            sa.text(
                "INSERT INTO tenants (id, user_id, api_key, is_active, is_admin) "
                "VALUES (:id, :user_id, :api_key, true, false)"
            ),
            {"id": TENANT_ID, "user_id": f"v2-recovery-{uuid4()}", "api_key": f"v2-{uuid4()}"},
        )
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DELETE FROM llm_gateway_skill_calls"))
            await connection.execute(sa.text("DELETE FROM llm_gateway_decisions"))
            await connection.execute(sa.text("DELETE FROM llm_gateway_events"))
            await connection.execute(sa.text("DELETE FROM llm_gateway_control_cycles"))
            await connection.execute(sa.text("DELETE FROM llm_gateway_sessions"))
            await connection.execute(
                sa.text("DELETE FROM action_tracking WHERE tenant_id = :tenant_id"),
                {"tenant_id": TENANT_ID},
            )
            await connection.execute(sa.text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": TENANT_ID})
        await engine.dispose()


async def _admit(repository: InboxRepository, *events: GatewayV2Event) -> None:
    result = await repository.accept_event_batch(IDENTITY, f"trace-{uuid4()}", events)
    assert result.received_event_ids == tuple(event.event_id for event in events)


async def _row(factory: async_sessionmaker[AsyncSession], event_id: str) -> sa.RowMapping:
    async with factory() as session:
        result = await session.execute(
            sa.text("SELECT * FROM llm_gateway_events WHERE gateway_id=:gateway_id AND event_id=:event_id"),
            {"gateway_id": IDENTITY.gateway_id, "event_id": event_id},
        )
        return result.mappings().one()


async def _runtime(factory: async_sessionmaker[AsyncSession], session_id: str = "session-1") -> sa.RowMapping:
    async with factory() as session:
        result = await session.execute(
            sa.text("SELECT * FROM llm_gateway_sessions WHERE gateway_id=:gateway_id AND session_id=:session_id"),
            {"gateway_id": IDENTITY.gateway_id, "session_id": session_id},
        )
        return result.mappings().one()


async def _cycle(
    factory: async_sessionmaker[AsyncSession],
    generation: int,
    session_id: str = "session-1",
) -> sa.RowMapping:
    async with factory() as session:
        result = await session.execute(
            sa.text(
                "SELECT * FROM llm_gateway_control_cycles "
                "WHERE gateway_id=:gateway_id AND session_id=:session_id AND control_generation=:generation"
            ),
            {"gateway_id": IDENTITY.gateway_id, "session_id": session_id, "generation": generation},
        )
        return result.mappings().one()


async def _expire_claim(factory: async_sessionmaker[AsyncSession], event_id: str) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_events SET lock_until=clock_timestamp() - interval '1 second' "
                "WHERE gateway_id=:gateway_id AND event_id=:event_id"
            ),
            {"gateway_id": IDENTITY.gateway_id, "event_id": event_id},
        )


async def _expire_decision_claim(factory: async_sessionmaker[AsyncSession], decision_id: str) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_decisions SET lock_until=clock_timestamp() - interval '1 second' "
                "WHERE gateway_id=:gateway_id AND decision_id=:decision_id"
            ),
            {"gateway_id": IDENTITY.gateway_id, "decision_id": decision_id},
        )


class _BlockingHooks(NoOpWorkerHooks):
    def __init__(self, target: str) -> None:
        self._target = target
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _block(self, target: str) -> None:
        if self._target != target:
            return
        self.entered.set()
        await self.release.wait()

    async def after_event_commit(self, event_ids: tuple[str, ...]) -> None:
        assert event_ids
        await self._block("after_event_commit")

    async def before_agent(self, event_id: str) -> None:
        assert event_id
        await self._block("before_agent")

    async def before_decision_http(self, decision_id: str) -> None:
        assert decision_id
        await self._block("before_decision_http")

    async def after_decision_http(self, decision_id: str) -> None:
        assert decision_id
        await self._block("after_decision_http")


def _skill_event(
    event_id: str,
    *,
    sequence: int,
    event_type: str,
    terminal: dict[str, object] | None = None,
) -> GatewayV2Event:
    payload: dict[str, object] = {
        "decisionId": "decision-1",
        "skillCallId": "call-1",
    }
    if event_type == "skill_finished":
        payload["terminal"] = terminal or {"status": "success"}
    return parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": event_type,
            "sessionId": "session-1",
            "controlGeneration": 1,
            "eventSequence": sequence,
            "occurredAtMs": 1_700_000_001_000 + sequence,
            "payload": payload,
        }
    )


async def _seed_decision(
    factory: async_sessionmaker[AsyncSession],
    *,
    cycle_id: UUID,
    source_event_id: UUID,
    decision_id: str = "decision-1",
    decision_lease_id: str = "lease-decision-1",
    action: str = "call_skill",
    status: str = "accepted",
    action_tracking_id: UUID | None = None,
) -> UUID:
    decision_row_id = uuid4()
    body = (
        {"action": "call_skill", "skillName": "jump", "schemaVersion": "v1", "arguments": {}}
        if action == "call_skill"
        else {"action": action}
    )
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_decisions (
                    id, tenant_id, cycle_id, source_event_id, action_tracking_id,
                    gateway_id, session_id, decision_id, decision_lease_id,
                    control_generation, state_version, action, request_body_json,
                    request_body_bytes, body_hash, status
                ) VALUES (
                    :id, :tenant_id, :cycle_id, :source_event_id, :action_tracking_id,
                    :gateway_id, 'session-1', :decision_id, :decision_lease_id,
                    1, 1, :action, CAST(:request_body_json AS jsonb),
                    :request_body_bytes, :body_hash, :status
                )
                """
            ),
            {
                "id": decision_row_id,
                "tenant_id": TENANT_ID,
                "cycle_id": cycle_id,
                "source_event_id": source_event_id,
                "action_tracking_id": action_tracking_id,
                "gateway_id": IDENTITY.gateway_id,
                "decision_id": decision_id,
                "decision_lease_id": decision_lease_id,
                "action": action,
                "request_body_json": json.dumps(body),
                "request_body_bytes": b"{}",
                "body_hash": "b" * 64,
                "status": status,
            },
        )
    return decision_row_id


async def test_after_ack_commit_hook_preserves_durable_batch_on_cancellation(session_factory) -> None:
    hooks = _BlockingHooks("after_event_commit")
    repository = InboxRepository(session_factory)
    service = EventService(repository, max_batch_size=10, hooks=hooks)
    event = _event("after-ack-event")
    envelope = GatewayV2BatchEnvelope.model_validate(
        {
            "traceId": "trace-after-ack",
            "gatewayId": IDENTITY.gateway_id,
            "contractVersion": "llm-gateway-http-v2",
            "sentAtMs": 1_700_000_000_100,
            "events": [event.model_dump(mode="json")],
        }
    )

    first = asyncio.create_task(service.accept_event_batch(IDENTITY, envelope))
    await asyncio.wait_for(hooks.entered.wait(), timeout=2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    retry = await EventService(repository, max_batch_size=10).accept_event_batch(IDENTITY, envelope)

    assert retry.received_event_ids == ()
    assert retry.duplicate_event_ids == (event.event_id,)
    row = await _row(session_factory, event.event_id)
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0


async def test_during_agent_hook_reclaims_same_event_without_duplicate_decision(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("during-agent-event"))
    hooks = _BlockingHooks("before_agent")
    calls: list[str] = []

    async def processor(event) -> EventProcessResult:
        calls.append(event.event_id)
        return EventProcessResult("succeeded")

    first_worker = EventWorker(
        repository=repository,
        processor=processor,
        status_registry=WorkerStatusRegistry(),
        worker_id="agent-old",
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
        hooks=hooks,
    )
    first_run = asyncio.create_task(first_worker.run_once())
    await asyncio.wait_for(hooks.entered.wait(), timeout=2)
    first_run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_run
    assert calls == []
    assert (await _row(session_factory, "during-agent-event"))["status"] == "processing"

    await _expire_claim(session_factory, "during-agent-event")
    second_worker = EventWorker(
        repository=repository,
        processor=processor,
        status_registry=WorkerStatusRegistry(),
        worker_id="agent-new",
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )

    assert await second_worker.run_once() == 1
    row = await _row(session_factory, "during-agent-event")
    assert row["status"] == "succeeded"
    assert row["attempt_count"] == 2
    assert calls == ["during-agent-event"]


class _DecisionClient:
    def __init__(self, response: DecisionClientResult) -> None:
        self.response = response
        self.bodies: list[bytes] = []

    async def send(self, *, action: str, raw_body: bytes) -> DecisionClientResult:
        assert action == "wait"
        self.bodies.append(raw_body)
        return self.response


def _decision_worker(
    repository: OutboxRepository,
    client: _DecisionClient,
    *,
    worker_id: str,
    hooks: NoOpWorkerHooks | None = None,
) -> DecisionWorker:
    return DecisionWorker(
        repository=repository,
        client=client,
        status_registry=WorkerStatusRegistry(),
        worker_id=worker_id,
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
        hooks=hooks or NoOpWorkerHooks(),
    )


async def _plan_wait_decision(session_factory, source_event_id: str):
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event(source_event_id))
    source = await inbox.claim_next_event(worker_id="planner", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: f"decision-{source_event_id}")
    planned = await outbox.plan_decision(source, context, GatewayV2WaitAction(reason="wait", waitMs=1_000))
    return outbox, planned


async def test_before_decision_http_hook_reclaims_without_first_http_call(session_factory) -> None:
    outbox, planned = await _plan_wait_decision(session_factory, "before-decision-http")
    response = DecisionClientResult(200, "accepted", "ok", None)
    client = _DecisionClient(response)
    hooks = _BlockingHooks("before_decision_http")
    first_run = asyncio.create_task(
        _decision_worker(outbox, client, worker_id="decision-old", hooks=hooks).run_once()
    )
    await asyncio.wait_for(hooks.entered.wait(), timeout=2)
    first_run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_run
    assert client.bodies == []

    await _expire_decision_claim(session_factory, planned.decision_id)
    assert await _decision_worker(outbox, client, worker_id="decision-new").run_once() == 1

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text("SELECT * FROM llm_gateway_decisions WHERE decision_id=:decision_id"),
                    {"decision_id": planned.decision_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "accepted"
    assert row["attempt_count"] == 2
    assert client.bodies == [planned.request_body_bytes]


async def test_after_gateway_accept_hook_retries_identical_http_body_and_old_cas_loses(
    session_factory,
) -> None:
    outbox, planned = await _plan_wait_decision(session_factory, "after-gateway-accept")
    response = DecisionClientResult(200, "accepted", "ok", None)
    client = _DecisionClient(response)
    hooks = _BlockingHooks("after_decision_http")
    first_run = asyncio.create_task(
        _decision_worker(outbox, client, worker_id="decision-old", hooks=hooks).run_once()
    )
    await asyncio.wait_for(hooks.entered.wait(), timeout=2)
    first_run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_run
    assert client.bodies == [planned.request_body_bytes]

    await _expire_decision_claim(session_factory, planned.decision_id)
    assert await _decision_worker(outbox, client, worker_id="decision-new").run_once() == 1

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text("SELECT * FROM llm_gateway_decisions WHERE decision_id=:decision_id"),
                    {"decision_id": planned.decision_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "accepted"
    assert row["attempt_count"] == 2
    assert row["decision_id"] == planned.decision_id
    assert bytes(row["request_body_bytes"]) == planned.request_body_bytes
    assert client.bodies == [planned.request_body_bytes, planned.request_body_bytes]


async def test_lease_context_update_requires_live_claim_and_fence(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("context-start", generation=1))
    first = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None
    first_context = build_gateway_v2_agent_context(first.event)
    assert await repository.persist_lease_context(first, first_context)
    assert await repository.complete_event(
        first,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    await _admit(repository, _event("context-next", generation=1, sequence=2))
    second = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert second is not None
    second_context = build_gateway_v2_agent_context(second.event)
    assert await repository.persist_lease_context(second, second_context)
    assert not await repository.persist_lease_context(first, first_context)

    cycle = await _cycle(session_factory, 1)
    assert cycle["latest_state_version"] == 2
    assert cycle["latest_decision_lease_id"] == "lease-session-1-1-2"
    assert cycle["latest_decision_context"]["eventId"] == "context-next"


async def test_planned_decision_reuses_exact_persisted_body_for_source_event(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-source", generation=1))
    source = await inbox.claim_next_event(worker_id="worker-plan", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-stable-db")

    first = await outbox.plan_decision(
        source,
        context,
        GatewayV2WaitAction(reason="wait", waitMs=1_500, ttlMs=9_000),
    )
    second = await outbox.plan_decision(
        source,
        context,
        GatewayV2NoOpAction(reason="different rerun output", ttlMs=7_000),
    )

    assert first.created is True
    assert second.created is False
    assert first.decision_id == second.decision_id == "decision-stable-db"
    assert first.request_body_json == second.request_body_json
    assert first.request_body_bytes == second.request_body_bytes
    assert first.body_hash == second.body_hash
    assert first.request_body_json["controlGeneration"] == source.control_generation
    assert first.request_body_json["action"] == "wait"


async def test_plan_transaction_rejects_changed_lease_context_without_writing(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-fenced", generation=1))
    source = await inbox.claim_next_event(worker_id="worker-plan", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_control_cycles "
                "SET latest_decision_lease_id='lease-newer', latest_state_version=99 "
                "WHERE id=:cycle_id"
            ),
            {"cycle_id": source.cycle_id},
        )
    outbox = OutboxRepository(session_factory)

    with pytest.raises(DecisionPlanFencedError):
        await outbox.plan_decision(
            source,
            context,
            GatewayV2WaitAction(reason="wait", waitMs=1_000),
        )

    async with session_factory() as session:
        count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert count == 0


async def test_tracking_and_planned_decision_commit_atomically(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-tracked", generation=1))
    source = await inbox.claim_next_event(worker_id="worker-plan", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    action = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "jump",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "tracked jump",
            "userId": "account-1",
            "actionType": "jump",
            "goalMetric": "jump_count",
            "goalValue": 2,
            "baselineValue": 1,
            "expectedHours": 2,
        }
    )

    planned = await OutboxRepository(session_factory).plan_decision(source, context, action)

    assert planned.action_tracking_id is not None
    assert "userId" not in planned.request_body_json
    assert "goalMetric" not in planned.request_body_json
    async with session_factory() as session:
        tracking = (
            (
                await session.execute(
                    sa.text("SELECT * FROM action_tracking WHERE id=:id"),
                    {"id": planned.action_tracking_id},
                )
            )
            .mappings()
            .one()
        )
        decision_tracking_id = await session.scalar(
            sa.text("SELECT action_tracking_id FROM llm_gateway_decisions WHERE id=:id"),
            {"id": planned.row_id},
        )
    assert tracking["status"] == "tracking"
    assert tracking["user_id"] == "account-1"
    assert tracking["goal_metric"] == "jump_count"
    assert decision_tracking_id == planned.action_tracking_id


async def test_two_source_events_cannot_consume_the_same_decision_lease(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    first_event = _event(
        "same-lease-first",
        sequence=1,
        decision_lease_id="shared-lease",
        state_version=1,
    )
    await _admit(inbox, first_event)
    first = await inbox.claim_next_event(worker_id="worker-first", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None
    first_context = build_gateway_v2_agent_context(first.event)
    assert await inbox.persist_lease_context(first, first_context)
    outbox = OutboxRepository(session_factory)
    await outbox.plan_decision(first, first_context, GatewayV2WaitAction(reason="first", waitMs=1_000))
    assert await inbox.complete_event(
        first,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    second_event = _event(
        "same-lease-second",
        sequence=2,
        decision_lease_id="shared-lease",
        state_version=1,
    )
    await _admit(inbox, second_event)
    second = await inbox.claim_next_event(worker_id="worker-second", claim_ttl_ms=30_000, max_attempts=3)
    assert second is not None
    second_context = build_gateway_v2_agent_context(second.event)
    assert await inbox.persist_lease_context(second, second_context)

    with pytest.raises(DecisionPlanConflictError) as raised:
        await outbox.plan_decision(
            second,
            second_context,
            GatewayV2WaitAction(reason="second", waitMs=1_000),
        )

    assert raised.value.category == "decision_lease_consumed"
    async with session_factory() as session:
        count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert count == 1


async def test_reclaimed_claim_fences_old_decision_planner(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-reclaimed"))
    old_claim = await inbox.claim_next_event(worker_id="worker-old", claim_ttl_ms=30_000, max_attempts=3)
    assert old_claim is not None
    old_context = build_gateway_v2_agent_context(old_claim.event)
    assert await inbox.persist_lease_context(old_claim, old_context)
    await _expire_claim(session_factory, old_claim.event_id)
    new_claim = await inbox.claim_next_event(worker_id="worker-new", claim_ttl_ms=30_000, max_attempts=3)
    assert new_claim is not None
    assert new_claim.claim_token != old_claim.claim_token

    with pytest.raises(DecisionPlanFencedError):
        await OutboxRepository(session_factory).plan_decision(
            old_claim,
            old_context,
            GatewayV2WaitAction(reason="stale", waitMs=1_000),
        )

    async with session_factory() as session:
        count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert count == 0


async def test_generation_switch_fences_old_decision_planner(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-old-generation", generation=1))
    old_claim = await inbox.claim_next_event(worker_id="worker-old", claim_ttl_ms=30_000, max_attempts=3)
    assert old_claim is not None
    old_context = build_gateway_v2_agent_context(old_claim.event)
    assert await inbox.persist_lease_context(old_claim, old_context)
    await _admit(inbox, _event("plan-new-generation", generation=2))
    new_claim = await inbox.claim_next_event(worker_id="worker-new", claim_ttl_ms=30_000, max_attempts=3)
    assert new_claim is not None
    assert new_claim.control_generation == 2

    with pytest.raises(DecisionPlanFencedError):
        await OutboxRepository(session_factory).plan_decision(
            old_claim,
            old_context,
            GatewayV2WaitAction(reason="stale generation", waitMs=1_000),
        )

    async with session_factory() as session:
        count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert count == 0


async def test_partial_goal_metadata_does_not_create_tracking(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-partial-tracking"))
    source = await inbox.claim_next_event(worker_id="worker-plan", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    action = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "jump",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "partial tracking",
            "goalMetric": "jump_count",
        }
    )

    planned = await OutboxRepository(session_factory).plan_decision(source, context, action)

    assert planned.action_tracking_id is None
    async with session_factory() as session:
        count = await session.scalar(
            sa.text("SELECT count(*) FROM action_tracking WHERE tenant_id=:tenant_id"),
            {"tenant_id": TENANT_ID},
        )
    assert count == 0


async def test_tracking_insert_failure_rolls_back_plan_transaction(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-tracking-failure"))
    source = await inbox.claim_next_event(worker_id="worker-plan", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    action = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "jump",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "tracking overflow",
            "userId": "account-1",
            "actionType": "jump",
            "goalMetric": "jump_count",
            "goalValue": 2,
            "baselineValue": 1,
            "expectedHours": 2**31,
        }
    )

    with pytest.raises(DecisionPlanUnavailableError):
        await OutboxRepository(session_factory).plan_decision(source, context, action)

    async with session_factory() as session:
        tracking_count = await session.scalar(
            sa.text("SELECT count(*) FROM action_tracking WHERE tenant_id=:tenant_id"),
            {"tenant_id": TENANT_ID},
        )
        decision_count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert tracking_count == 0
    assert decision_count == 0


async def test_outbox_insert_failure_rolls_back_tracking(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.integration.llm_gateway_v2 import outbox_repository as outbox_module

    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("plan-outbox-failure"))
    source = await inbox.claim_next_event(worker_id="worker-plan", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    action = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "jump",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "tracked jump",
            "userId": "account-1",
            "actionType": "jump",
            "goalMetric": "jump_count",
            "goalValue": 2,
            "baselineValue": 1,
            "expectedHours": 1,
        }
    )
    monkeypatch.setattr(outbox_module, "_INSERT_PLANNED_DECISION", sa.text("SELECT 1 / 0"))

    with pytest.raises(DecisionPlanUnavailableError):
        await OutboxRepository(session_factory).plan_decision(source, context, action)

    async with session_factory() as session:
        tracking_count = await session.scalar(
            sa.text("SELECT count(*) FROM action_tracking WHERE tenant_id=:tenant_id"),
            {"tenant_id": TENANT_ID},
        )
        decision_count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert tracking_count == 0
    assert decision_count == 0


async def test_reusing_decision_id_with_different_body_marks_existing_manual(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("decision-id-first", sequence=1))
    first = await inbox.claim_next_event(worker_id="worker-first", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None
    first_context = build_gateway_v2_agent_context(first.event)
    assert await inbox.persist_lease_context(first, first_context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "fixed-decision-id")
    first_plan = await outbox.plan_decision(
        first,
        first_context,
        GatewayV2WaitAction(reason="first", waitMs=1_000),
    )
    assert await inbox.complete_event(
        first,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    await _admit(inbox, _event("decision-id-second", sequence=2))
    second = await inbox.claim_next_event(worker_id="worker-second", claim_ttl_ms=30_000, max_attempts=3)
    assert second is not None
    second_context = build_gateway_v2_agent_context(second.event)
    assert await inbox.persist_lease_context(second, second_context)

    with pytest.raises(DecisionPlanConflictError) as raised:
        await outbox.plan_decision(
            second,
            second_context,
            GatewayV2NoOpAction(reason="different body"),
        )

    assert raised.value.category == "decision_id_body_conflict"
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text("SELECT status, error_category FROM llm_gateway_decisions WHERE id=:id"),
                    {"id": first_plan.row_id},
                )
            )
            .mappings()
            .one()
        )
        count = await session.scalar(sa.text("SELECT count(*) FROM llm_gateway_decisions"))
    assert row["status"] == "manual"
    assert row["error_category"] == "decision_id_body_conflict"
    assert count == 1


async def test_accepted_response_first_then_skill_event_merges_one_call(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("accepted-first-source"))
    source = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-1")
    await outbox.plan_decision(
        source,
        context,
        GatewayV2CallSkillAction.model_validate(
            {
                "action": "call_skill",
                "skillName": "jump",
                "schemaVersion": "v1",
                "arguments": {},
                "reason": "jump",
            }
        ),
    )
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    claimed = await outbox.claim_next_decision(
        worker_id="decision-worker",
        claim_ttl_ms=30_000,
        max_attempts=3,
    )
    assert claimed is not None
    assert await outbox.record_decision_response(
        claimed,
        DecisionClientResult(200, "accepted", "ok", "call-1"),
    )

    await _admit(inbox, _skill_event("accepted-first-started", sequence=2, event_type="skill_started"))
    started = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert started is not None
    assert (
        await TerminalRepository(session_factory).record_skill_started(started)
    ).disposition is MutationDisposition.APPLIED

    async with session_factory() as session:
        calls = (
            (
                await session.execute(
                    sa.text(
                        "SELECT decision_id, skill_call_id, skill_name, status "
                        "FROM llm_gateway_skill_calls WHERE gateway_id=:gateway_id"
                    ),
                    {"gateway_id": IDENTITY.gateway_id},
                )
            )
            .mappings()
            .all()
        )
    assert len(calls) == 1
    assert dict(calls[0]) == {
        "decision_id": "decision-1",
        "skill_call_id": "call-1",
        "skill_name": "jump",
        "status": "started",
    }


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [("skill_started", "started"), ("skill_finished", "succeeded")],
)
async def test_skill_event_first_then_late_accepted_merges_same_call(
    session_factory,
    event_type: str,
    expected_status: str,
) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("event-first-source"))
    source = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-1")
    await outbox.plan_decision(
        source,
        context,
        GatewayV2CallSkillAction.model_validate(
            {
                "action": "call_skill",
                "skillName": "jump",
                "schemaVersion": "v1",
                "arguments": {},
                "reason": "jump",
            }
        ),
    )
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    claimed = await outbox.claim_next_decision(
        worker_id="decision-worker",
        claim_ttl_ms=30_000,
        max_attempts=3,
    )
    assert claimed is not None

    await _admit(inbox, _skill_event("event-first-terminal", sequence=2, event_type=event_type))
    skill_event = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert skill_event is not None
    terminal_repository = TerminalRepository(session_factory)
    if event_type == "skill_started":
        result = await terminal_repository.record_skill_started(skill_event)
    else:
        result = await terminal_repository.record_skill_finished(skill_event)
    assert result.disposition is MutationDisposition.APPLIED

    assert await outbox.record_decision_response(
        claimed,
        DecisionClientResult(200, "accepted", "ok", "call-1"),
    )

    async with session_factory() as session:
        decision_status = await session.scalar(
            sa.text("SELECT status FROM llm_gateway_decisions WHERE decision_id='decision-1'")
        )
        calls = (
            (await session.execute(sa.text("SELECT status FROM llm_gateway_skill_calls WHERE skill_call_id='call-1'")))
            .scalars()
            .all()
        )
    assert decision_status == "accepted"
    assert calls == [expected_status]


async def test_expired_sending_reclaim_reuses_body_and_fences_old_response(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("decision-reclaim-source"))
    source = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-1")
    planned = await outbox.plan_decision(
        source,
        context,
        GatewayV2WaitAction(reason="wait", waitMs=1_000),
    )
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    first = await outbox.claim_next_decision(worker_id="decision-old", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None
    await _expire_decision_claim(session_factory, "decision-1")
    second = await outbox.claim_next_decision(worker_id="decision-new", claim_ttl_ms=30_000, max_attempts=3)
    assert second is not None

    assert first.request_body_bytes == second.request_body_bytes == planned.request_body_bytes
    assert first.body_hash == second.body_hash == planned.body_hash
    assert first.claim_token != second.claim_token
    assert second.attempt_count == 2
    accepted = DecisionClientResult(200, "accepted", "ok", None)
    assert not await outbox.record_decision_response(first, accepted)
    assert await outbox.record_decision_response(second, accepted)


async def test_http_idempotency_conflict_moves_decision_to_manual(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("http-conflict-source"))
    source = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-1")
    await outbox.plan_decision(source, context, GatewayV2WaitAction(reason="wait", waitMs=1_000))
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    claimed = await outbox.claim_next_decision(worker_id="decision-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert claimed is not None

    assert await outbox.record_decision_response(
        claimed,
        DecisionClientResult(409, "rejected", "idempotency_key_conflict", None),
    )

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        "SELECT status, response_status, response_reason, error_category "
                        "FROM llm_gateway_decisions WHERE decision_id='decision-1'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert dict(row) == {
        "status": "manual",
        "response_status": "rejected",
        "response_reason": "idempotency_key_conflict",
        "error_category": "idempotency_key_conflict",
    }


async def test_decision_transport_failures_are_bounded_by_max_attempts(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("bounded-decision-source"))
    source = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-1")
    await outbox.plan_decision(source, context, GatewayV2WaitAction(reason="wait", waitMs=1_000))
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    for attempt in range(1, 4):
        claimed = await outbox.claim_next_decision(
            worker_id=f"decision-worker-{attempt}",
            claim_ttl_ms=30_000,
            max_attempts=3,
        )
        assert claimed is not None
        assert claimed.attempt_count == attempt
        assert await outbox.complete_decision_failure(
            claimed,
            error_stage="http",
            error_category="timeout",
            max_attempts=3,
            retry_base_ms=100,
            retry_max_ms=1_000,
        )
        async with session_factory() as session:
            status = await session.scalar(
                sa.text("SELECT status FROM llm_gateway_decisions WHERE decision_id='decision-1'")
            )
        assert status == ("dead_letter" if attempt == 3 else "retryable_failed")
        if attempt < 3:
            async with session_factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE llm_gateway_decisions "
                        "SET next_attempt_at=clock_timestamp() - interval '1 second' "
                        "WHERE decision_id='decision-1'"
                    )
                )

    assert await outbox.count_decision_dead_letters() == 1
    assert (
        await outbox.claim_next_decision(
            worker_id="decision-worker-extra",
            claim_ttl_ms=30_000,
            max_attempts=3,
        )
        is None
    )


async def test_exhausted_sending_claim_is_swept_to_dead_letter(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("sweep-decision-source"))
    source = await inbox.claim_next_event(worker_id="event-worker", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    context = build_gateway_v2_agent_context(source.event)
    assert await inbox.persist_lease_context(source, context)
    outbox = OutboxRepository(session_factory, decision_id_factory=lambda: "decision-1")
    await outbox.plan_decision(source, context, GatewayV2WaitAction(reason="wait", waitMs=1_000))
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    claimed = await outbox.claim_next_decision(
        worker_id="decision-worker",
        claim_ttl_ms=30_000,
        max_attempts=1,
    )
    assert claimed is not None
    await _expire_decision_claim(session_factory, "decision-1")

    assert await outbox.sweep_expired_decision_claims(max_attempts=1) == 1
    assert await outbox.sweep_expired_decision_claims(max_attempts=1) == 0

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        "SELECT status, attempt_count, error_stage, error_category "
                        "FROM llm_gateway_decisions WHERE decision_id='decision-1'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert dict(row) == {
        "status": "dead_letter",
        "attempt_count": 1,
        "error_stage": "worker",
        "error_category": "claim_expired",
    }


async def test_terminal_transition_applies_tracking_effect_once(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("terminal-source", generation=1))
    source = await inbox.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    tracking_id = uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "INSERT INTO action_tracking (id, tenant_id, user_id, action_type, status) "
                "VALUES (:id, :tenant_id, 'user-1', 'jump', 'tracking')"
            ),
            {"id": tracking_id, "tenant_id": TENANT_ID},
        )
    await _seed_decision(
        session_factory,
        cycle_id=source.cycle_id,
        source_event_id=source.row_id,
        action_tracking_id=tracking_id,
    )
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    await _admit(inbox, _skill_event("skill-started", sequence=2, event_type="skill_started"))
    started = await inbox.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert started is not None
    terminal_repository = TerminalRepository(session_factory)
    assert (await terminal_repository.record_skill_started(started)).disposition is MutationDisposition.APPLIED
    assert await inbox.complete_event(
        started,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    await _admit(inbox, _skill_event("skill-finished", sequence=3, event_type="skill_finished"))
    finished = await inbox.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert finished is not None
    assert (await terminal_repository.record_skill_finished(finished)).disposition is MutationDisposition.APPLIED
    assert (await terminal_repository.record_skill_finished(finished)).disposition is MutationDisposition.IDEMPOTENT

    async with session_factory() as session:
        call = (
            (
                await session.execute(
                    sa.text(
                        "SELECT * FROM llm_gateway_skill_calls WHERE gateway_id=:gateway_id AND skill_call_id='call-1'"
                    ),
                    {"gateway_id": IDENTITY.gateway_id},
                )
            )
            .mappings()
            .one()
        )
        tracking_status = await session.scalar(
            sa.text("SELECT status FROM action_tracking WHERE id=:id"),
            {"id": tracking_id},
        )
    assert (call["status"], call["effect_status"]) == ("succeeded", "applied")
    assert call["terminal_event_id"] == finished.row_id
    assert tracking_status == "completed"


async def test_session_stop_converges_event_outbox_and_stop_call_atomically(session_factory) -> None:
    inbox = InboxRepository(session_factory)
    await _admit(inbox, _event("stop-source", generation=1))
    source = await inbox.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert source is not None
    stop_decision_id = await _seed_decision(
        session_factory,
        cycle_id=source.cycle_id,
        source_event_id=source.row_id,
        action="stop_hosting",
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_skill_calls (
                    tenant_id, decision_row_id, gateway_id, session_id, decision_id,
                    skill_call_id, skill_name, status
                ) VALUES (
                    :tenant_id, :decision_row_id, :gateway_id, 'session-1',
                    'decision-1', 'stop-call-1', 'stop_hosting', 'pending'
                )
                """
            ),
            {
                "tenant_id": TENANT_ID,
                "decision_row_id": stop_decision_id,
                "gateway_id": IDENTITY.gateway_id,
            },
        )
        auxiliary_event_id = uuid4()
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_events (
                    id, tenant_id, cycle_id, gateway_id, session_id, event_id,
                    event_type, control_generation, event_sequence, content_hash,
                    event_body, trace_id, status
                ) VALUES (
                    :id, :tenant_id, :cycle_id, :gateway_id, 'session-1',
                    'aux-source', 'observation_updated', 1, 99, :content_hash,
                    CAST(:event_body AS jsonb), 'trace-aux', 'succeeded'
                )
                """
            ),
            {
                "id": auxiliary_event_id,
                "tenant_id": TENANT_ID,
                "cycle_id": source.cycle_id,
                "gateway_id": IDENTITY.gateway_id,
                "content_hash": "c" * 64,
                "event_body": json.dumps(_event("aux", sequence=99).model_dump(mode="json", by_alias=True)),
            },
        )
    await _seed_decision(
        session_factory,
        cycle_id=source.cycle_id,
        source_event_id=auxiliary_event_id,
        decision_id="decision-unsent",
        decision_lease_id="lease-unsent",
        action="wait",
        status="planned",
    )
    assert await inbox.complete_event(
        source,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    await _admit(
        inbox,
        _event(
            "stop-event",
            generation=1,
            sequence=2,
            event_type="session_stopped",
            stop_reason="stop_hosting_requested",
        ),
    )
    stopped = await inbox.claim_next_event(worker_id="worker-stop", claim_ttl_ms=30_000, max_attempts=3)
    assert stopped is not None
    outbox = OutboxRepository(session_factory)

    assert (await outbox.close_generation(stopped)).disposition is MutationDisposition.APPLIED
    assert not await inbox.complete_event(
        stopped,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    async with session_factory() as session:
        event_status = await session.scalar(
            sa.text("SELECT status FROM llm_gateway_events WHERE id=:id"),
            {"id": stopped.row_id},
        )
        unsent_status = await session.scalar(
            sa.text("SELECT status FROM llm_gateway_decisions WHERE decision_id='decision-unsent'"),
        )
        call = (
            (await session.execute(sa.text("SELECT * FROM llm_gateway_skill_calls WHERE skill_call_id='stop-call-1'")))
            .mappings()
            .one()
        )
    assert event_status == "succeeded"
    assert unsent_status == "cancelled"
    assert (call["status"], call["reason"], call["effect_status"]) == (
        "succeeded",
        "stop_hosting_requested",
        "not_applicable",
    )
    assert call["terminal_event_id"] == stopped.row_id
    assert (await _cycle(session_factory, 1))["status"] == "stopped"
    assert (await _runtime(session_factory))["status"] == "stopped"


async def test_gap_blocks_later_sequence_and_partition_advances_in_order(session_factory) -> None:
    repository = InboxRepository(session_factory)
    later = _event("event-2", sequence=2)
    await _admit(repository, later)

    assert await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3) is None

    started = _event("event-1", sequence=1)
    await _admit(repository, started)
    first = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None and first.event_id == "event-1"
    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3) is None

    assert await repository.complete_event(
        first,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    second = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert second is not None and second.event_id == "event-2"


class _BarrierClaimRepository(InboxRepository):
    def __init__(self, session_factory, barrier: asyncio.Barrier) -> None:
        super().__init__(session_factory)
        self._claim_barrier = barrier
        self.after_lock_calls = 0

    async def _after_claim_candidate_lock(self, candidate) -> None:
        del candidate
        self.after_lock_calls += 1
        await asyncio.wait_for(self._claim_barrier.wait(), timeout=5)


async def test_two_workers_contend_and_only_one_claims_a_partition(session_factory) -> None:
    seed = InboxRepository(session_factory)
    await _admit(seed, _event("event-1"))
    barrier = asyncio.Barrier(2)
    first = _BarrierClaimRepository(session_factory, barrier)
    second = _BarrierClaimRepository(session_factory, barrier)

    claims = await asyncio.gather(
        first.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3),
        second.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3),
    )

    assert sum(claim is not None for claim in claims) == 1
    row = await _row(session_factory, "event-1")
    assert row["status"] == "processing"
    assert row["attempt_count"] == 1
    assert first.after_lock_calls == second.after_lock_calls == 1


async def test_different_sessions_can_be_claimed_concurrently(session_factory) -> None:
    seed = InboxRepository(session_factory)
    await _admit(
        seed,
        _event("event-s1", session_id="session-1"),
        _event("event-s2", session_id="session-2"),
    )
    barrier = asyncio.Barrier(2)
    first = _BarrierClaimRepository(session_factory, barrier)
    second = _BarrierClaimRepository(session_factory, barrier)

    claims = await asyncio.gather(
        first.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3),
        second.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3),
    )

    assert {claim.session_id for claim in claims if claim is not None} == {"session-1", "session-2"}
    assert first.after_lock_calls == second.after_lock_calls == 1


async def test_new_generation_started_increments_fence_once_and_supersedes_old_cycle(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("g1-start", generation=1))
    g1 = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert g1 is not None
    assert await repository.complete_event(
        g1,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(repository, _event("g2-start", generation=2))

    g2 = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert g2 is not None and g2.control_generation == 2
    runtime = await _runtime(session_factory)
    assert (runtime["current_generation"], runtime["fence_version"], runtime["status"]) == (2, 2, "active")
    assert (await _cycle(session_factory, 1))["status"] == "superseded"
    assert (await _cycle(session_factory, 2))["status"] == "active"

    assert await repository.complete_event(
        g2,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    duplicate = await repository.accept_event_batch(IDENTITY, "trace-g2-duplicate", (_event("g2-start", generation=2),))
    assert duplicate.received_event_ids == ()
    assert duplicate.duplicate_event_ids == ("g2-start",)
    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3) is None
    assert (await _runtime(session_factory))["fence_version"] == 2


async def test_future_non_started_event_does_not_activate_generation(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("g1-start", generation=1))
    claim = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert claim is not None
    assert await repository.complete_event(
        claim,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(repository, _event("g2-observation", generation=2, sequence=2))

    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3) is None
    runtime = await _runtime(session_factory)
    assert (runtime["current_generation"], runtime["fence_version"]) == (1, 1)
    assert (await _cycle(session_factory, 2))["status"] in {"pending", "manual"}
    assert (await _row(session_factory, "g2-observation"))["status"] in {"pending", "manual"}


async def test_activating_generation_does_not_supersede_a_newer_pending_cycle(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("g1-start", generation=1))
    g1 = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert g1 is not None
    assert await repository.complete_event(
        g1,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(repository, _event("g3-observation", generation=3, sequence=2))
    await _admit(repository, _event("g2-start", generation=2))

    g2 = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)

    assert g2 is not None and g2.control_generation == 2
    assert (await _cycle(session_factory, 3))["status"] == "pending"


async def test_waiting_future_cycle_is_isolated_without_starving_other_sessions(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("s1-g1-start", session_id="session-1", generation=1))
    current = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert current is not None
    assert await repository.complete_event(
        current,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(
        repository,
        _event("s1-g2-observation", session_id="session-1", generation=2, sequence=2),
        _event("s2-g1-start", session_id="session-2", generation=1),
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_control_cycles SET next_event_sequence=2 "
                "WHERE gateway_id=:gateway_id AND session_id='session-1' AND control_generation=2"
            ),
            {"gateway_id": IDENTITY.gateway_id},
        )

    claim = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)

    assert claim is not None and claim.session_id == "session-2"
    assert (await _row(session_factory, "s1-g2-observation"))["status"] == "manual"
    assert (await _cycle(session_factory, 2, session_id="session-1"))["status"] == "manual"


async def test_stale_generation_is_superseded_without_returning_it_to_processor(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("g1-start", generation=1), _event("g1-next", generation=1, sequence=2))
    first = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None and first.event_id == "g1-start"
    assert await repository.complete_event(
        first,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(repository, _event("g2-start", generation=2))
    new_generation = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert new_generation is not None and new_generation.event_id == "g2-start"

    assert await repository.complete_event(
        new_generation,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_control_cycles "
                "SET latest_state_version=9, latest_decision_lease_id='new-lease', "
                'latest_decision_context=\'{"marker": "new"}\'::jsonb '
                "WHERE gateway_id=:gateway_id AND session_id='session-1' AND control_generation=2"
            ),
            {"gateway_id": IDENTITY.gateway_id},
        )
    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3) is None
    assert (await _row(session_factory, "g1-next"))["status"] == "superseded"
    old_cycle = await _cycle(session_factory, 1)
    assert old_cycle["next_event_sequence"] == 3
    runtime = await _runtime(session_factory)
    assert (runtime["current_generation"], runtime["fence_version"]) == (2, 2)
    current_cycle = await _cycle(session_factory, 2)
    assert current_cycle["latest_state_version"] == 9
    assert current_cycle["latest_decision_lease_id"] == "new-lease"
    assert current_cycle["latest_decision_context"] == {"marker": "new"}


async def test_old_generation_stop_cannot_stop_current_generation(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("g1-start", generation=1))
    g1 = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert g1 is not None
    await repository.complete_event(
        g1,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(repository, _event("g2-start", generation=2))
    g2 = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert g2 is not None
    await repository.complete_event(
        g2,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _admit(repository, _event("g1-stop", generation=1, sequence=2, event_type="session_stopped"))

    assert await repository.claim_next_event(worker_id="worker-3", claim_ttl_ms=30_000, max_attempts=3) is None
    runtime = await _runtime(session_factory)
    assert (runtime["current_generation"], runtime["status"]) == (2, "active")
    assert (await _row(session_factory, "g1-stop"))["status"] == "superseded"


async def test_current_generation_stop_closes_only_its_cycle(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(
        repository,
        _event("start", generation=1),
        _event("stop", generation=1, sequence=2, event_type="session_stopped"),
    )
    started = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert started is not None
    await repository.complete_event(
        started,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    stopped = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert stopped is not None and stopped.event_type == "session_stopped"
    await repository.complete_event(
        stopped,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    assert (await _runtime(session_factory))["status"] == "stopped"
    assert (await _cycle(session_factory, 1))["status"] == "stopped"


async def test_expired_claim_is_fenced_and_bounded(session_factory) -> None:
    repository = InboxRepository(session_factory)
    event = _event("event-reclaim")
    await _admit(repository, event)
    first = await repository.claim_next_event(worker_id="worker-old", claim_ttl_ms=30_000, max_attempts=2)
    assert first is not None
    original_body = first.event.model_dump(mode="json")
    original_hash = first.content_hash
    await _expire_claim(session_factory, event.event_id)

    second = await repository.claim_next_event(worker_id="worker-new", claim_ttl_ms=30_000, max_attempts=2)
    assert second is not None
    assert second.row_id == first.row_id
    assert second.claim_token != first.claim_token
    assert second.attempt_count == 2
    assert second.event.model_dump(mode="json") == original_body
    assert second.content_hash == original_hash
    assert not await repository.complete_event(
        first,
        EventProcessResult("succeeded"),
        max_attempts=2,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    await _expire_claim(session_factory, event.event_id)

    assert await repository.sweep_expired_claims(max_attempts=2) == 1
    assert await repository.claim_next_event(worker_id="worker-third", claim_ttl_ms=30_000, max_attempts=2) is None
    row = await _row(session_factory, event.event_id)
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == 2
    assert row["claim_token"] is None


async def test_exhausted_stale_claim_is_superseded_instead_of_dead_lettered(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("g1-start", generation=1))
    stale = await repository.claim_next_event(worker_id="worker-old", claim_ttl_ms=30_000, max_attempts=1)
    assert stale is not None
    await _admit(repository, _event("g2-start", generation=2))
    current = await repository.claim_next_event(worker_id="worker-new", claim_ttl_ms=30_000, max_attempts=3)
    assert current is not None and current.control_generation == 2
    await _expire_claim(session_factory, stale.event_id)

    assert await repository.sweep_expired_claims(max_attempts=1) == 0
    assert (await _row(session_factory, stale.event_id))["status"] == "superseded"
    assert (await _cycle(session_factory, 1))["next_event_sequence"] == 2


async def test_worker_renews_live_claim_so_second_worker_cannot_reclaim(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("event-long-running"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def processor(event) -> EventProcessResult:
        del event
        entered.set()
        await release.wait()
        return EventProcessResult("succeeded")

    worker = EventWorker(
        repository=repository,
        processor=processor,
        status_registry=WorkerStatusRegistry(),
        worker_id="worker-live",
        poll_interval_ms=10,
        claim_ttl_ms=150,
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), timeout=2)
    await asyncio.sleep(0.35)

    competing = await repository.claim_next_event(
        worker_id="worker-competing",
        claim_ttl_ms=150,
        max_attempts=3,
    )
    release.set()

    assert competing is None
    assert await asyncio.wait_for(running, timeout=2) == 1
    assert (await _row(session_factory, "event-long-running"))["status"] == "succeeded"


async def test_repeated_processor_crashes_are_swept_at_attempt_limit(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("event-crash-loop"))
    status = WorkerStatusRegistry()

    async def crashing_processor(event) -> EventProcessResult:
        del event
        raise RuntimeError("simulated processor crash")

    worker = EventWorker(
        repository=repository,
        processor=crashing_processor,
        status_registry=status,
        worker_id="worker-crash",
        poll_interval_ms=10,
        claim_ttl_ms=30_000,
        max_attempts=2,
        retry_base_ms=100,
        retry_max_ms=1_000,
        max_parallelism=1,
    )

    assert await worker.run_once() == 1
    assert (await _row(session_factory, "event-crash-loop"))["attempt_count"] == 1
    await _expire_claim(session_factory, "event-crash-loop")
    assert await worker.run_once() == 1
    assert (await _row(session_factory, "event-crash-loop"))["attempt_count"] == 2
    await _expire_claim(session_factory, "event-crash-loop")

    assert await worker.run_once() == 0
    row = await _row(session_factory, "event-crash-loop")
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == 2
    assert status.snapshot().dead_letter_count == 1
    assert status.snapshot().degraded is True


async def test_retry_backoff_does_not_increment_attempt_until_reclaim(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("event-retry"))
    first = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None and first.attempt_count == 1
    before = datetime.now(UTC)
    assert await repository.complete_event(
        first,
        EventProcessResult("retryable_failed", error_stage="agent", error_category="timeout"),
        max_attempts=3,
        retry_base_ms=1_000,
        retry_max_ms=10_000,
    )
    row = await _row(session_factory, "event-retry")
    assert row["status"] == "retryable_failed"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] >= before + timedelta(seconds=0.9)
    assert row["claim_token"] is None
    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3) is None

    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE llm_gateway_events SET next_attempt_at=clock_timestamp() - interval '1 second'"),
        )
    second = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert second is not None and second.attempt_count == 2
    second_before = datetime.now(UTC)
    assert await repository.complete_event(
        second,
        EventProcessResult("retryable_failed", error_stage="agent", error_category="timeout"),
        max_attempts=3,
        retry_base_ms=1_000,
        retry_max_ms=1_500,
    )
    second_row = await _row(session_factory, "event-retry")
    assert second_row["attempt_count"] == 2
    assert second_row["next_attempt_at"] >= second_before + timedelta(seconds=1.4)


async def test_retry_at_attempt_limit_dead_letters_and_blocks_partition(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("event-1"), _event("event-2", sequence=2))
    claim = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=1)
    assert claim is not None
    assert await repository.complete_event(
        claim,
        EventProcessResult("retryable_failed", error_stage="agent", error_category="timeout"),
        max_attempts=1,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    assert (await _row(session_factory, "event-1"))["status"] == "dead_letter"
    assert await repository.count_dead_letters() == 1
    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=1) is None
    assert (await _row(session_factory, "event-2"))["status"] == "pending"


async def test_manual_result_blocks_partition_without_retry(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("event-1"), _event("event-2", sequence=2))
    claim = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert claim is not None
    assert await repository.complete_event(
        claim,
        EventProcessResult("manual", error_stage="contract", error_category="unsupported"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    assert (await _row(session_factory, "event-1"))["status"] == "manual"
    assert (await _cycle(session_factory, 1))["status"] == "manual"
    assert await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3) is None


@asynccontextmanager
async def _failing_completion_trigger(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION llm_gateway_v2_completion_fault() RETURNS trigger AS $$
                BEGIN
                    IF NEW.status = 'succeeded' THEN
                        RAISE EXCEPTION 'completion fault' USING ERRCODE = '40001';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        await session.execute(
            sa.text(
                "CREATE TRIGGER llm_gateway_v2_completion_fault BEFORE UPDATE ON llm_gateway_events "
                "FOR EACH ROW EXECUTE FUNCTION llm_gateway_v2_completion_fault()"
            )
        )
    try:
        yield
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                sa.text("DROP TRIGGER IF EXISTS llm_gateway_v2_completion_fault ON llm_gateway_events")
            )
            await session.execute(sa.text("DROP FUNCTION IF EXISTS llm_gateway_v2_completion_fault()"))


async def test_database_completion_failure_preserves_processing_until_reclaim(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await _admit(repository, _event("event-completion-failure"))
    first = await repository.claim_next_event(worker_id="worker-1", claim_ttl_ms=30_000, max_attempts=3)
    assert first is not None

    with pytest.raises(Exception, match="completion"):
        async with _failing_completion_trigger(session_factory):
            await repository.complete_event(
                first,
                EventProcessResult("succeeded"),
                max_attempts=3,
                retry_base_ms=100,
                retry_max_ms=1_000,
            )

    row = await _row(session_factory, "event-completion-failure")
    assert row["status"] == "processing"
    assert row["claim_token"] == first.claim_token
    await _expire_claim(session_factory, "event-completion-failure")
    reclaimed = await repository.claim_next_event(worker_id="worker-2", claim_ttl_ms=30_000, max_attempts=3)
    assert reclaimed is not None
    assert reclaimed.claim_token != first.claim_token
    assert reclaimed.attempt_count == 2
