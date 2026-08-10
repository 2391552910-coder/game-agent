from __future__ import annotations

import asyncio
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
from src.core.integration.llm_gateway_v2.event_service import EventService
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
            "controlGeneration": 1,
            "eventSequence": sequence,
            "stateVersion": 0,
            "decisionLeaseId": None,
            "occurredAtMs": 1_700_000_000_000 + sequence,
            "payload": {
                "sessionId": "session-1",
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "supported": True,
                "text": "你好",
                "serverTimeMs": 1_700_000_000_000 + sequence,
                "conversation": {
                    "conversationId": "conv-100-200-1",
                    "pairKey": "100:200",
                    "speakerRoleId": 100,
                    "targetRoleId": 200,
                    "brainUsername": "conv-100",
                    "historyRounds": [],
                    "completedRounds": 0,
                    "maxRounds": 6,
                    "expiresAtMs": 1_800_000_000_000,
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


async def test_hosted_chat_event_is_admitted_once_without_a_decision_lease(session_factory) -> None:
    repository = InboxRepository(session_factory)
    started = _event("session-started")
    chat = _chat_event()

    first = await repository.accept_event_batch(IDENTITY, "trace-chat", (started, chat))
    duplicate = await repository.accept_event_batch(IDENTITY, "trace-chat-retry", (chat,))

    assert first.received_event_ids == ("session-started", "chat-event-1")
    assert duplicate.received_event_ids == ()
    assert duplicate.duplicate_event_ids == ("chat-event-1",)
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT event_type, event_body FROM llm_gateway_events "
                    "WHERE event_id = 'chat-event-1'"
                )
            )
        ).mappings().one()
    assert row["event_type"] == "chat_received"
    assert row["event_body"]["stateVersion"] == 0
    assert row["event_body"]["decisionLeaseId"] is None


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


async def test_batch_internal_content_conflict_fails_before_database(session_factory) -> None:
    repository = InboxRepository(session_factory)
    original = _event("event-1", sequence=2, reason="first")
    changed = _event("event-1", sequence=2, reason="changed")

    with pytest.raises(EventAdmissionConflict) as caught:
        await repository.accept_event_batch(IDENTITY, "trace-1", (original, changed))

    assert caught.value.event_id == "event-1"
    assert await _counts(session_factory) == (0, 0, 0)


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


async def test_recoverable_integrity_error_omits_only_current_event(session_factory) -> None:
    repository = InboxRepository(session_factory)
    first = _event("event-1", sequence=2, reason="first")
    same_partition_sequence = _event("event-2", sequence=2, reason="second")
    later = _event("event-3", sequence=3)

    acceptance = await repository.accept_event_batch(
        IDENTITY,
        "trace-1",
        (first, same_partition_sequence, later),
    )

    assert acceptance.received_event_ids == ("event-1", "event-3")
    assert acceptance.duplicate_event_ids == ()
    async with session_factory() as session:
        result = await session.execute(sa.text("SELECT event_id FROM llm_gateway_events ORDER BY event_id"))
        ids = result.scalars().all()
    assert ids == ["event-1", "event-3"]


@asynccontextmanager
async def _fault_trigger(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION llm_gateway_v2_test_fault() RETURNS trigger AS $$
                BEGIN
                    IF NEW.event_id = 'fault-data' THEN
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


async def test_recoverable_data_error_omits_only_current_event(session_factory) -> None:
    # A single connection keeps the temporary trigger visible to repository sessions.
    engine = session_factory.kw.get("bind")
    assert engine is not None
    connection = await engine.connect()
    factory = async_sessionmaker(connection, expire_on_commit=False, autoflush=False)
    try:
        async with _fault_trigger(factory):
            repository = InboxRepository(factory)
            acceptance = await repository.accept_event_batch(
                IDENTITY,
                "trace-1",
                (
                    _event("event-1", sequence=2),
                    _event("fault-data", sequence=2, session_id="session-fault"),
                    _event("event-3", sequence=3),
                ),
            )
        assert acceptance.received_event_ids == ("event-1", "event-3")
        assert acceptance.duplicate_event_ids == ()
        async with session_factory() as session:
            orphan_sessions = await session.scalar(
                sa.text("SELECT count(*) FROM llm_gateway_sessions WHERE session_id = 'session-fault'")
            )
            orphan_cycles = await session.scalar(
                sa.text("SELECT count(*) FROM llm_gateway_control_cycles WHERE session_id = 'session-fault'")
            )
        assert (orphan_sessions, orphan_cycles) == (0, 0)
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

    async def _load_existing(self, session, gateway_id, prepared):
        existing = await super()._load_existing(session, gateway_id, prepared)
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
