from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from src.api.routes import gateway_v2
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2BatchEnvelope,
    GatewayV2Event,
    parse_gateway_v2_event,
)
from src.core.integration.llm_gateway_v2.decision_service import build_gateway_v2_agent_context
from src.core.integration.llm_gateway_v2.event_service import EventService
from src.core.integration.llm_gateway_v2.event_worker import EventProcessResult
from src.core.integration.llm_gateway_v2.inbox_repository import (
    EventAdmissionConflict,
    EventAdmissionUnavailable,
    InboxRepository,
)

pytestmark = pytest.mark.asyncio

TENANT_ID = UUID("00000000-0000-0000-0000-000000000061")
IDENTITY = InboundGatewayIdentity("gateway-events", "gateway-1", TENANT_ID)


def _event(
    event_id: str = "event-1",
    *,
    sequence: int = 1,
    session_id: str = "session-1",
    reason: str = "stopped",
) -> GatewayV2Event:
    if sequence == 1:
        event_type = "session_started"
        payload = {
            "reason": "decision_requested",
            "lease": {
                "sessionId": session_id,
                "controlGeneration": 1,
                "decisionLeaseId": "lease-1",
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
        decision_lease_id = "lease-1"
    else:
        event_type = "session_stopped"
        payload = {
            "reason": reason,
            "stoppedAtMs": 1_700_000_000_000 + sequence,
        }
        decision_lease_id = None
    return parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": event_type,
            "sessionId": session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": 1,
            "decisionLeaseId": decision_lease_id,
            "occurredAtMs": 1_700_000_000_000 + sequence,
            "payload": payload,
        }
    )


def _chat_event(event_id: str = "chat-event-1", *, sequence: int = 2) -> GatewayV2Event:
    return parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": "chat_received",
            "sessionId": "session-1",
            "stateVersion": 0,
            "decisionLeaseId": None,
            "occurredAtMs": 1_700_000_000_000 + sequence,
            "payload": {
                "sessionId": "session-1",
                "schemaVersion": "v1",
                "contentType": 0,
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "supported": True,
                "text": "你好",
                "serverTimeMs": 1_700_000_000_000 + sequence,
            },
        }
    )


def _skill_finished_event(event_id: str = "event-finished", *, sequence: int = 2) -> GatewayV2Event:
    session_id = "session-1"
    lease = {
        "sessionId": session_id,
        "controlGeneration": 1,
        "decisionLeaseId": "lease-2",
        "stateVersion": 2,
        "leaseKind": "observation",
        "allowedActions": ["wait"],
        "allowedSkillName": None,
        "allowedSkillNames": [],
        "parentSkillName": None,
    }
    return parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": "skill_finished",
            "sessionId": session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": 2,
            "decisionLeaseId": "lease-2",
            "occurredAtMs": 1_700_000_000_000 + sequence,
            "payload": {
                "decisionId": "decision-1",
                "skillCallId": "call-1",
                "skillName": "jump",
                "status": "success",
                "reason": "completed",
                "failureCategory": None,
                "retryable": False,
                "startedAtMs": 1_700_000_000_000,
                "finishedAtMs": 1_700_000_000_001,
                "lease": lease,
                "decisionContext": {
                    "session": {"status": "active"},
                    "availableSkills": [],
                    "skillArgumentHints": [],
                    "lastSkillResult": {"status": "success"},
                },
            },
        }
    )


def _observation_event(
    event_id: str,
    *,
    sequence: int,
    session_id: str = "session-1",
) -> GatewayV2Event:
    lease_id = f"lease-{sequence}"
    lease = {
        "sessionId": session_id,
        "controlGeneration": 1,
        "decisionLeaseId": lease_id,
        "stateVersion": sequence,
        "leaseKind": "observation",
        "allowedActions": ["wait"],
        "allowedSkillName": None,
        "allowedSkillNames": [],
        "parentSkillName": None,
    }
    return parse_gateway_v2_event(
        {
            "eventId": event_id,
            "eventType": "observation_updated",
            "sessionId": session_id,
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": sequence,
            "decisionLeaseId": lease_id,
            "occurredAtMs": 1_700_000_000_000 + sequence,
            "payload": {
                "reason": "state_changed",
                "lease": lease,
                "decisionContext": {
                    "session": {"status": "active", "stateVersion": sequence},
                    "availableSkills": [],
                    "skillArgumentHints": [],
                    "lastSkillResult": None,
                },
            },
        }
    )


@pytest.fixture(scope="module", autouse=True)
def _upgrade_schema(migration_config) -> None:
    command.upgrade(migration_config, "head")


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
        await connection.execute(sa.text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": TENANT_ID})
        await connection.execute(
            sa.text(
                "INSERT INTO tenants (id, user_id, api_key, is_active, is_admin) "
                "VALUES (:id, :user_id, :api_key, true, false)"
            ),
            {"id": TENANT_ID, "user_id": f"v2-{uuid4()}", "api_key": f"v2-{uuid4()}"},
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
            await connection.execute(sa.text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": TENANT_ID})
        await engine.dispose()


async def _counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    async with factory() as session:
        values = []
        for table in ("llm_gateway_sessions", "llm_gateway_control_cycles", "llm_gateway_events"):
            values.append(await session.scalar(sa.text(f"SELECT count(*) FROM {table}")))
    return tuple(values)  # type: ignore[return-value]


async def test_new_duplicate_mixed_batch_and_retry_are_durable(session_factory) -> None:
    repository = InboxRepository(session_factory)
    first = _event("event-1")
    second = _event("event-2", sequence=2)

    accepted = await repository.accept_event_batch(IDENTITY, "trace-1", (first,))
    mixed = await repository.accept_event_batch(IDENTITY, "trace-2", (first, second))
    retry = await repository.accept_event_batch(IDENTITY, "trace-retry", (first, second))

    assert accepted.received_event_ids == ("event-1",)
    assert mixed.received_event_ids == ("event-2",)
    assert mixed.duplicate_event_ids == ("event-1",)
    assert retry.received_event_ids == ()
    assert retry.duplicate_event_ids == ("event-1", "event-2")
    assert await _counts(session_factory) == (1, 1, 2)
    async with session_factory() as session:
        assert await session.scalar(sa.text("SELECT status FROM llm_gateway_control_cycles")) == "pending"
        assert await session.scalar(sa.text("SELECT status FROM llm_gateway_events LIMIT 1")) == "pending"


async def test_stale_event_is_discarded_before_it_can_be_claimed(session_factory) -> None:
    stale_repository = InboxRepository(session_factory, event_stale_after_seconds=480)
    stale_event = _event("stale-event").model_copy(
        update={"occurred_at_ms": int((time.time() - 600) * 1_000)}
    )
    await stale_repository.accept_event_batch(IDENTITY, "trace-stale", (stale_event,))
    async with session_factory() as session:
        event_row = (
            await session.execute(
                sa.text(
                    "SELECT id, cycle_id FROM llm_gateway_events WHERE event_id='stale-event'"
                )
            )
        ).mappings().one()
        decision_row_id = uuid4()
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_decisions (
                    id, tenant_id, cycle_id, source_event_id,
                    gateway_id, session_id, decision_id, decision_lease_id,
                    control_generation, state_version, action, request_body_json,
                    request_body_bytes, body_hash, status
                ) VALUES (
                    :id, :tenant_id, :cycle_id, :source_event_id,
                    :gateway_id, 'session-1', 'stale-decision', 'stale-lease',
                    1, 1, 'call_skill', CAST(:request_body_json AS jsonb),
                    :request_body_bytes, :body_hash, 'planned'
                )
                """
            ),
            {
                "id": decision_row_id,
                "tenant_id": TENANT_ID,
                "cycle_id": event_row["cycle_id"],
                "source_event_id": event_row["id"],
                "gateway_id": IDENTITY.gateway_id,
                "request_body_json": '{"action":"call_skill","skillName":"jump"}',
                "request_body_bytes": b"{}",
                "body_hash": "d" * 64,
            },
        )
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_skill_calls (
                    tenant_id, decision_row_id, gateway_id, session_id,
                    decision_id, skill_call_id, skill_name, status
                ) VALUES (
                    :tenant_id, :decision_row_id, :gateway_id, 'session-1',
                    'stale-decision', 'stale-call', 'jump', 'pending'
                )
                """
            ),
            {
                "tenant_id": TENANT_ID,
                "decision_row_id": decision_row_id,
                "gateway_id": IDENTITY.gateway_id,
            },
        )
        await session.commit()

    assert await stale_repository.discard_stale_events(max_age_seconds=480) == 1
    assert await stale_repository.claim_next_event(
        worker_id="worker-stale",
        claim_ttl_ms=30_000,
        max_attempts=5,
    ) is None

    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT status, error_category FROM llm_gateway_events "
                    "WHERE event_id='stale-event'"
                )
            )
        ).mappings().one()
        skill_call = (
            await session.execute(
                sa.text(
                    "SELECT status, reason, failure_category "
                    "FROM llm_gateway_skill_calls WHERE skill_call_id='stale-call'"
                )
            )
        ).mappings().one()
    assert (row["status"], row["error_category"]) == (
        "superseded",
        "stale_event_discarded",
    )
    assert (skill_call["status"], skill_call["reason"], skill_call["failure_category"]) == (
        "cancelled",
        "stale_event_discarded",
        None,
    )


async def test_idle_session_is_stopped_and_fenced(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await repository.accept_event_batch(IDENTITY, "trace-idle", (_event("idle-start"),))
    started = await repository.claim_next_event(
        worker_id="worker-idle",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert started is not None
    assert await repository.complete_event(
        started,
        EventProcessResult("succeeded"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    decision_row_id = uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_decisions (
                    id, tenant_id, cycle_id, source_event_id,
                    gateway_id, session_id, decision_id, decision_lease_id,
                    control_generation, state_version, action, request_body_json,
                    request_body_bytes, body_hash, status
                ) VALUES (
                    :id, :tenant_id, :cycle_id, :source_event_id,
                    :gateway_id, 'session-1', 'idle-decision', 'idle-lease',
                    1, 1, 'call_skill', CAST(:request_body_json AS jsonb),
                    :request_body_bytes, :body_hash, 'planned'
                )
                """
            ),
            {
                "id": decision_row_id,
                "tenant_id": TENANT_ID,
                "cycle_id": started.cycle_id,
                "source_event_id": started.row_id,
                "gateway_id": IDENTITY.gateway_id,
                "request_body_json": '{"action":"call_skill","skillName":"jump"}',
                "request_body_bytes": b"{}",
                "body_hash": "c" * 64,
            },
        )
        await session.execute(
            sa.text(
                """
                INSERT INTO llm_gateway_skill_calls (
                    tenant_id, decision_row_id, gateway_id, session_id,
                    decision_id, skill_call_id, skill_name, status
                ) VALUES (
                    :tenant_id, :decision_row_id, :gateway_id, 'session-1',
                    'idle-decision', 'idle-call', 'jump', 'pending'
                )
                """
            ),
            {
                "tenant_id": TENANT_ID,
                "decision_row_id": decision_row_id,
                "gateway_id": IDENTITY.gateway_id,
            },
        )
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_sessions "
                "SET last_event_at=clock_timestamp() - interval '601 seconds'"
            )
        )
        await session.commit()

    assert await repository.stop_idle_sessions(idle_timeout_seconds=600) == 1

    async with session_factory() as session:
        runtime = (
            await session.execute(
                sa.text("SELECT status, fence_version FROM llm_gateway_sessions")
            )
        ).mappings().one()
        cycle = (
            await session.execute(
                sa.text("SELECT status FROM llm_gateway_control_cycles")
            )
        ).mappings().one()
        skill_call = (
            await session.execute(
                sa.text(
                    "SELECT status, reason, failure_category "
                    "FROM llm_gateway_skill_calls WHERE skill_call_id='idle-call'"
                )
            )
        ).mappings().one()
    assert (runtime["status"], runtime["fence_version"]) == ("stopped", 2)
    assert cycle["status"] == "stopped"
    assert (skill_call["status"], skill_call["reason"], skill_call["failure_category"]) == (
        "cancelled",
        "gateway_session_inactive",
        None,
    )


async def test_new_session_start_is_not_starved_by_historical_active_observation(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await repository.accept_event_batch(
        IDENTITY,
        "trace-old-session",
        (_event("old-start", session_id="old-session"),),
    )
    old_start = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert old_start is not None
    assert await repository.complete_event(
        old_start,
        EventProcessResult("succeeded"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    await repository.accept_event_batch(
        IDENTITY,
        "trace-mixed-backlog",
        (
            _observation_event("old-observation", sequence=2, session_id="old-session"),
            _event("new-start", session_id="new-session"),
        ),
    )

    claimed = await repository.claim_next_event(
        worker_id="worker-2",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )

    assert claimed is not None
    assert claimed.event_id == "new-start"


async def test_manual_cycle_pending_events_are_cleaned_and_excluded_from_queue(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await repository.accept_event_batch(
        IDENTITY,
        "trace-manual-cleanup",
        (_event("manual-start"), _observation_event("manual-observation", sequence=2)),
    )
    started = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert started is not None
    assert await repository.complete_event(
        started,
        EventProcessResult("manual", error_stage="agent", error_category="unsupported"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE llm_gateway_events "
                "SET status='processing', attempt_count=1, lock_until=clock_timestamp() + interval '1 hour' "
                "WHERE event_id='manual-observation'"
            )
        )
        await session.commit()

    assert await repository.sweep_expired_claims(max_attempts=5) == 0
    queue = await repository.queue_metrics()
    async with session_factory() as session:
        observation_status = await session.scalar(
            sa.text("SELECT status FROM llm_gateway_events WHERE event_id='manual-observation'")
        )

    assert observation_status == "manual"
    assert queue.depth == 0


async def test_superseded_cycle_convergence_events_are_cleaned_and_excluded_from_queue(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await repository.accept_event_batch(
        IDENTITY,
        "trace-superseded-cleanup",
        (_event("superseded-start"), _observation_event("superseded-observation", sequence=2)),
    )
    started = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert started is not None
    assert await repository.complete_event(
        started,
        EventProcessResult("succeeded"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE llm_gateway_control_cycles SET status='superseded'")
        )
        await session.commit()

    assert await repository.sweep_expired_claims(max_attempts=5) == 0
    queue = await repository.queue_metrics(max_attempts=5)
    async with session_factory() as session:
        observation_status = await session.scalar(
            sa.text(
                "SELECT status FROM llm_gateway_events "
                "WHERE event_id='superseded-observation'"
            )
        )

    assert observation_status == "superseded"
    assert queue.depth == 0


async def test_standalone_hosted_chat_event_is_admitted_without_a_control_cycle(session_factory) -> None:
    repository = InboxRepository(session_factory)

    accepted = await repository.accept_event_batch(IDENTITY, "trace-standalone-chat", (_chat_event(),))

    assert accepted.received_event_ids == ("chat-event-1",)
    assert await _counts(session_factory) == (1, 1, 1)
    claimed = await repository.claim_next_event(
        worker_id="chat-worker",
        claim_ttl_ms=30_000,
        max_attempts=3,
    )
    assert claimed is not None
    assert claimed.event_type == "chat_received"
    assert await repository.complete_event(
        claimed,
        EventProcessResult("succeeded"),
        max_attempts=3,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    async with session_factory() as session:
        assert await session.scalar(
            sa.text("SELECT status FROM llm_gateway_events WHERE event_id='chat-event-1'")
        ) == "succeeded"


async def test_hosted_chat_event_is_admitted_once_without_a_decision_lease(session_factory) -> None:
    repository = InboxRepository(session_factory)
    started = _event("session-started")
    chat = _chat_event()

    first = await repository.accept_event_batch(IDENTITY, "trace-chat", (chat, started))
    duplicate = await repository.accept_event_batch(IDENTITY, "trace-chat-retry", (chat,))

    assert first.received_event_ids == ("chat-event-1", "session-started")
    assert duplicate.received_event_ids == ()
    assert duplicate.duplicate_event_ids == ("chat-event-1",)
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT event_type, control_generation, event_sequence, event_body "
                    "FROM llm_gateway_events "
                    "WHERE event_id = 'chat-event-1'"
                )
            )
        ).mappings().one()
    assert row["event_type"] == "chat_received"
    assert row["control_generation"] == 1
    assert row["event_sequence"] >= 2**62
    assert row["event_body"]["stateVersion"] == 0
    assert row["event_body"]["decisionLeaseId"] is None
    assert "controlGeneration" not in row["event_body"]
    assert "eventSequence" not in row["event_body"]

    claimed_start = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert claimed_start is not None
    assert claimed_start.event_type == "session_started"
    assert await repository.complete_event(
        claimed_start,
        EventProcessResult("succeeded"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    claimed_chat = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert claimed_chat is not None
    assert claimed_chat.event_type == "chat_received"
    assert await repository.complete_event(
        claimed_chat,
        EventProcessResult("succeeded"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )
    async with session_factory() as session:
        next_sequence = await session.scalar(
            sa.text("SELECT next_event_sequence FROM llm_gateway_control_cycles")
        )
    assert next_sequence == 2


async def test_complete_event_body_hash_and_trace_are_persisted(session_factory) -> None:
    repository = InboxRepository(session_factory)
    event = _event("event-complete")

    await repository.accept_event_batch(IDENTITY, "trace-complete", (event,))

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        """
                    SELECT tenant_id, gateway_id, session_id, event_id, event_type,
                           control_generation, event_sequence, content_hash,
                           event_body, trace_id, status
                    FROM llm_gateway_events
                    WHERE gateway_id = :gateway_id AND event_id = :event_id
                    """
                    ),
                    {"gateway_id": IDENTITY.gateway_id, "event_id": event.event_id},
                )
            )
            .mappings()
            .one()
        )

    assert row["tenant_id"] == TENANT_ID
    assert row["gateway_id"] == IDENTITY.gateway_id
    assert row["session_id"] == event.session_id
    assert row["event_type"] == event.event_type
    assert row["control_generation"] == event.control_generation
    assert row["event_sequence"] == event.event_sequence
    assert len(row["content_hash"]) == 64
    assert row["event_body"] == event.model_dump(mode="json")
    assert row["trace_id"] == "trace-complete"
    assert row["status"] == "pending"


async def test_batch_duplicates_are_deduplicated_in_first_seen_order(session_factory) -> None:
    repository = InboxRepository(session_factory)
    first = _event("event-1")
    second = _event("event-2", sequence=2)

    acceptance = await repository.accept_event_batch(
        IDENTITY,
        "trace-1",
        (first, first, second, first, second),
    )

    assert acceptance.received_event_ids == ("event-1", "event-2")
    assert acceptance.duplicate_event_ids == ()
    assert await _counts(session_factory) == (1, 1, 2)


async def test_fifty_event_batch_uses_constant_database_statements(
    session_factory,
    verified_test_postgres_url: URL,
) -> None:
    engine = create_async_engine(verified_test_postgres_url, poolclass=sa.pool.NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement.strip())

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    events = tuple(
        _event(f"bulk-event-{index}", session_id=f"bulk-session-{index}")
        for index in range(50)
    )
    try:
        acceptance = await InboxRepository(factory).accept_event_batch(
            IDENTITY,
            "trace-bulk-50",
            events,
        )
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
        await engine.dispose()

    assert acceptance.received_event_ids == tuple(event.event_id for event in events)
    assert acceptance.duplicate_event_ids == ()
    assert len(statements) <= 4
    assert not any(statement.startswith(("SAVEPOINT", "RELEASE SAVEPOINT")) for statement in statements)
    assert await _counts(session_factory) == (50, 50, 50)


async def test_batch_internal_content_conflict_fails_before_database(session_factory) -> None:
    repository = InboxRepository(session_factory)
    original = _event("event-1", sequence=2, reason="first")
    changed = _event("event-1", sequence=2, reason="changed")

    with pytest.raises(EventAdmissionConflict) as caught:
        await repository.accept_event_batch(IDENTITY, "trace-1", (original, changed))

    assert caught.value.event_id == "event-1"
    assert await _counts(session_factory) == (0, 0, 0)


async def test_session_stopped_preempts_slow_event_and_invalidates_old_claim(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await repository.accept_event_batch(
        IDENTITY,
        "trace-stop-priority",
        (_event("event-start"), _event("event-stop", sequence=2)),
    )

    started = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert started is not None
    assert started.event_type == "session_started"

    stopped = await repository.claim_next_event(
        worker_id="worker-2",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert stopped is not None
    assert stopped.event_type == "session_stopped"
    assert await repository.complete_event(
        stopped,
        EventProcessResult("succeeded"),
        max_attempts=5,
        retry_base_ms=100,
        retry_max_ms=1_000,
    )

    assert not await repository.renew_event_claim(started, claim_ttl_ms=30_000)
    async with session_factory() as session:
        started_status = await session.scalar(
            sa.text("SELECT status FROM llm_gateway_events WHERE event_id = 'event-start'")
        )
    assert started_status == "superseded"


async def test_new_terminal_lease_supersedes_older_inflight_event(session_factory) -> None:
    repository = InboxRepository(session_factory)
    started_event = _event("event-start")
    finished_event = _skill_finished_event()
    await repository.accept_event_batch(
        IDENTITY,
        "trace-new-lease",
        (started_event, finished_event),
    )

    started = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert started is not None
    assert await repository.persist_lease_context(
        started,
        build_gateway_v2_agent_context(started.event),
    )

    finished = await repository.claim_next_event(
        worker_id="worker-2",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert finished is not None
    assert finished.event_type == "skill_finished"
    assert await repository.persist_lease_context(
        finished,
        build_gateway_v2_agent_context(finished.event),
    )

    assert not await repository.renew_event_claim(started, claim_ttl_ms=30_000)
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT status FROM llm_gateway_events WHERE event_id = 'event-start'"
                )
            )
        ).scalar_one()
    assert row == "superseded"


async def test_latest_observation_coalesces_older_model_work(session_factory) -> None:
    repository = InboxRepository(session_factory)
    await repository.accept_event_batch(
        IDENTITY,
        "trace-observation-coalesce",
        (
            _event("event-start"),
            _observation_event("event-observation-2", sequence=2),
            _observation_event("event-observation-3", sequence=3),
        ),
    )
    started = await repository.claim_next_event(
        worker_id="worker-1",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert started is not None
    assert await repository.persist_lease_context(
        started,
        build_gateway_v2_agent_context(started.event),
    )

    latest = await repository.claim_next_event(
        worker_id="worker-2",
        claim_ttl_ms=30_000,
        max_attempts=5,
    )
    assert latest is not None
    assert latest.event_id == "event-observation-3"
    assert await repository.persist_lease_context(
        latest,
        build_gateway_v2_agent_context(latest.event),
    )

    async with session_factory() as session:
        statuses = dict(
            (
                await session.execute(
                    sa.text(
                        "SELECT event_id, status FROM llm_gateway_events "
                        "WHERE event_id IN ('event-start', 'event-observation-2')"
                    )
                )
            ).all()
        )
    assert statuses == {
        "event-start": "superseded",
        "event-observation-2": "superseded",
    }


async def test_persisted_content_conflict_rolls_back_other_new_events(session_factory) -> None:
    repository = InboxRepository(session_factory)
    original = _event("event-1", sequence=2, reason="first")
    await repository.accept_event_batch(IDENTITY, "trace-1", (original,))

    new_event = _event("event-2", sequence=3)
    conflicting = _event("event-1", sequence=2, reason="changed")
    with pytest.raises(EventAdmissionConflict):
        await repository.accept_event_batch(IDENTITY, "trace-2", (new_event, conflicting))

    async with session_factory() as session:
        result = await session.execute(sa.text("SELECT event_id FROM llm_gateway_events ORDER BY event_id"))
        ids = result.scalars().all()
    assert ids == ["event-1"]


async def test_partition_sequence_conflict_rolls_back_the_whole_batch(session_factory) -> None:
    repository = InboxRepository(session_factory)
    first = _event("event-1", sequence=2, reason="first")
    same_partition_sequence = _event("event-2", sequence=2, reason="second")
    later = _event("event-3", sequence=3)

    with pytest.raises(EventAdmissionConflict) as caught:
        await repository.accept_event_batch(
            IDENTITY,
            "trace-1",
            (first, same_partition_sequence, later),
        )

    assert caught.value.event_id == "event-2"
    assert await _counts(session_factory) == (0, 0, 0)


@asynccontextmanager
async def _fault_trigger(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION llm_gateway_v2_test_fault() RETURNS trigger AS $$
                BEGIN
                    IF NEW.event_id = 'fault-timeout' THEN
                        PERFORM pg_sleep(0.2);
                    ELSIF NEW.event_id = 'fault-data' THEN
                        RAISE EXCEPTION 'data fault' USING ERRCODE = '22003';
                    ELSIF NEW.event_id = 'fault-deadlock' THEN
                        RAISE EXCEPTION 'deadlock fault' USING ERRCODE = '40P01';
                    ELSIF NEW.event_id = 'fault-serialization' THEN
                        RAISE EXCEPTION 'serialization fault' USING ERRCODE = '40001';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        await session.execute(sa.text("DROP TRIGGER IF EXISTS llm_gateway_v2_test_fault ON llm_gateway_events"))
        await session.execute(
            sa.text(
                """
                CREATE TRIGGER llm_gateway_v2_test_fault
                BEFORE INSERT ON llm_gateway_events
                FOR EACH ROW EXECUTE FUNCTION llm_gateway_v2_test_fault();
                """
            )
        )
    try:
        yield
    finally:
        async with factory() as session, session.begin():
            await session.execute(sa.text("DROP TRIGGER IF EXISTS llm_gateway_v2_test_fault ON llm_gateway_events"))
            await session.execute(sa.text("DROP FUNCTION IF EXISTS llm_gateway_v2_test_fault()"))


async def test_data_error_rolls_back_the_whole_batch(session_factory) -> None:
    # A single connection keeps the temporary trigger visible to repository sessions.
    engine = session_factory.kw.get("bind")
    assert engine is not None
    connection = await engine.connect()
    factory = async_sessionmaker(connection, expire_on_commit=False, autoflush=False)
    try:
        async with _fault_trigger(factory):
            repository = InboxRepository(factory)
            with pytest.raises(EventAdmissionUnavailable):
                await repository.accept_event_batch(
                    IDENTITY,
                    "trace-1",
                    (
                        _event("event-1", sequence=2),
                        _event("fault-data", sequence=2, session_id="session-fault"),
                        _event("event-3", sequence=3),
                    ),
                )
        assert await _counts(session_factory) == (0, 0, 0)
    finally:
        await connection.close()


async def test_statement_timeout_rolls_back_and_connection_remains_usable(session_factory) -> None:
    engine = session_factory.kw.get("bind")
    assert engine is not None
    connection = await engine.connect()
    factory = async_sessionmaker(connection, expire_on_commit=False, autoflush=False)
    try:
        async with _fault_trigger(factory):
            repository = InboxRepository(factory, statement_timeout_seconds=0.05)
            with pytest.raises(EventAdmissionUnavailable):
                await repository.accept_event_batch(
                    IDENTITY,
                    "trace-timeout",
                    (_event("fault-timeout"),),
                )
        assert await connection.scalar(sa.text("SELECT 1")) == 1
        assert await _counts(session_factory) == (0, 0, 0)
    finally:
        await connection.close()


@pytest.mark.parametrize("event_id", ["fault-deadlock", "fault-serialization"])
async def test_deadlock_and_serialization_fail_the_whole_batch(session_factory, event_id: str) -> None:
    engine = session_factory.kw.get("bind")
    assert engine is not None
    connection = await engine.connect()
    factory = async_sessionmaker(connection, expire_on_commit=False, autoflush=False)
    try:
        async with _fault_trigger(factory):
            repository = InboxRepository(factory)
            with pytest.raises(EventAdmissionUnavailable):
                await repository.accept_event_batch(
                    IDENTITY,
                    "trace-1",
                    (_event("event-1", sequence=2), _event(event_id, sequence=3)),
                )
        assert await _counts(session_factory) == (0, 0, 0)
    finally:
        await connection.close()


async def test_database_connection_failure_is_whole_batch_unavailable() -> None:
    unavailable_engine = create_async_engine(
        "postgresql+asyncpg://myagent:myagent@127.0.0.1:1/myagent_test_unreachable",
        poolclass=sa.pool.NullPool,
        connect_args={"timeout": 0.2},
    )
    repository = InboxRepository(async_sessionmaker(unavailable_engine, expire_on_commit=False))
    try:
        with pytest.raises(EventAdmissionUnavailable):
            await repository.accept_event_batch(IDENTITY, "trace-1", (_event(),))
    finally:
        await unavailable_engine.dispose()


class _CommitFailingSession(AsyncSession):
    async def commit(self) -> None:
        await super().rollback()
        raise sa.exc.OperationalError("COMMIT", {}, RuntimeError("commit result unknown"))


async def test_commit_failure_returns_unavailable_and_acks_nothing(session_factory) -> None:
    engine = session_factory.kw.get("bind")
    assert engine is not None
    failing_factory = async_sessionmaker(
        engine,
        class_=_CommitFailingSession,
        expire_on_commit=False,
        autoflush=False,
    )
    repository = InboxRepository(failing_factory)

    with pytest.raises(EventAdmissionUnavailable):
        await repository.accept_event_batch(IDENTITY, "trace-1", (_event(),))

    assert await _counts(session_factory) == (0, 0, 0)


class _CommitSucceededButConfirmationLostSession(AsyncSession):
    async def commit(self) -> None:
        await super().commit()
        raise sa.exc.OperationalError("COMMIT", {}, RuntimeError("commit confirmation lost"))


async def test_unknown_commit_result_retries_as_duplicate(session_factory) -> None:
    engine = session_factory.kw.get("bind")
    assert engine is not None
    uncertain_factory = async_sessionmaker(
        engine,
        class_=_CommitSucceededButConfirmationLostSession,
        expire_on_commit=False,
        autoflush=False,
    )
    event = _event("event-commit-unknown")

    with pytest.raises(EventAdmissionUnavailable):
        await InboxRepository(uncertain_factory).accept_event_batch(IDENTITY, "trace-1", (event,))

    retry = await InboxRepository(session_factory).accept_event_batch(IDENTITY, "trace-retry", (event,))
    assert retry.received_event_ids == ()
    assert retry.duplicate_event_ids == ("event-commit-unknown",)
    assert await _counts(session_factory) == (1, 1, 1)


async def test_http_response_loss_retries_committed_batch_as_duplicates(session_factory) -> None:
    repository = InboxRepository(session_factory)
    service = EventService(repository, max_batch_size=10)
    identity = IDENTITY
    envelope = GatewayV2BatchEnvelope.model_validate(
        {
            "traceId": "trace-response-lost",
            "gatewayId": identity.gateway_id,
            "contractVersion": "llm-gateway-http-v2",
            "sentAtMs": 1_700_000_000_100,
            "events": [
                _event("event-response-1").model_dump(mode="json"),
                _event("event-response-2", sequence=2).model_dump(mode="json"),
            ],
        }
    )
    body = envelope.model_dump_json().encode("utf-8")
    app = FastAPI()
    app.include_router(gateway_v2.router)
    route_settings = SimpleNamespace(
        llm_gateway_v2_enabled=True,
        llm_gateway_v2_max_event_batch_size=10,
    )

    async def admit(
        resolved_identity: InboundGatewayIdentity,
        parsed_envelope: GatewayV2BatchEnvelope,
    ):
        return await service.accept_event_batch(resolved_identity, parsed_envelope)

    request_delivered = False

    async def receive() -> dict[str, object]:
        nonlocal request_delivered
        if request_delivered:
            return {"type": "http.disconnect"}
        request_delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def fail_response_body(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise ConnectionError("client disconnected before ACK body")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/gateway/v2/events",
        "raw_path": b"/api/gateway/v2/events",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }

    with (
        patch.object(gateway_v2, "settings", route_settings),
        patch.object(gateway_v2, "verify_inbound_hmac", return_value=identity.app_id),
        patch.object(gateway_v2, "resolve_inbound_identity", return_value=identity),
        patch.object(gateway_v2, "accept_gateway_event_batch", side_effect=admit),
    ):
        with pytest.raises(ConnectionError, match="disconnected"):
            await app(scope, receive, fail_response_body)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/gateway/v2/events",
                content=body,
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 200
    assert response.json()["receivedEventIds"] == []
    assert response.json()["duplicateEventIds"] == ["event-response-1", "event-response-2"]
    assert await _counts(session_factory) == (1, 1, 2)


class _BarrierInboxRepository(InboxRepository):
    def __init__(self, session_factory, barrier: asyncio.Barrier) -> None:
        super().__init__(session_factory)
        self._barrier = barrier
        self._waited = False

    async def _load_existing(self, session, gateway_id, prepared):
        existing = await super()._load_existing(session, gateway_id, prepared)
        if not self._waited:
            self._waited = True
            await asyncio.wait_for(self._barrier.wait(), timeout=5)
        return existing


async def test_concurrent_same_content_returns_one_received_and_one_duplicate(session_factory) -> None:
    event = _event("event-race")
    barrier = asyncio.Barrier(2)
    first = _BarrierInboxRepository(session_factory, barrier)
    second = _BarrierInboxRepository(session_factory, barrier)

    results = await asyncio.gather(
        first.accept_event_batch(IDENTITY, "trace-1", (event,)),
        second.accept_event_batch(IDENTITY, "trace-2", (event,)),
    )

    assert sorted((result.received_event_ids, result.duplicate_event_ids) for result in results) == [
        ((), ("event-race",)),
        (("event-race",), ()),
    ]
    assert await _counts(session_factory) == (1, 1, 1)


async def test_concurrent_different_content_returns_one_received_and_one_conflict(session_factory) -> None:
    barrier = asyncio.Barrier(2)
    first = _BarrierInboxRepository(session_factory, barrier)
    second = _BarrierInboxRepository(session_factory, barrier)
    original = _event("event-race", sequence=2, reason="first")
    changed = _event("event-race", sequence=2, reason="changed")

    results = await asyncio.gather(
        first.accept_event_batch(IDENTITY, "trace-1", (original,)),
        second.accept_event_batch(IDENTITY, "trace-2", (changed,)),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, EventAdmissionConflict) for result in results) == 1
    assert await _counts(session_factory) == (1, 1, 1)
