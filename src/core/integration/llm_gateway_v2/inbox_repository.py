from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from asyncpg.exceptions import DataError as AsyncpgDataError  # type: ignore[import-untyped]
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.infrastructure.db import async_session_factory
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.canonical import event_content_hash
from src.core.integration.llm_gateway_v2.contracts import GatewayV2Event, parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.event_worker import (
    ClaimedGatewayEvent,
    EventProcessResult,
    GenerationDisposition,
    classify_generation,
)


@dataclass(frozen=True)
class BatchAcceptance:
    received_event_ids: tuple[str, ...]
    duplicate_event_ids: tuple[str, ...]


class EventAdmissionConflict(Exception):  # noqa: N818 - protocol-domain name
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__()


class EventAdmissionUnavailable(Exception):  # noqa: N818 - protocol-domain name
    def __init__(self) -> None:
        super().__init__()


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


@dataclass(frozen=True)
class _PreparedEvent:
    event: GatewayV2Event
    content_hash: str


_SELECT_EXISTING = sa.text(
    """
    SELECT event_id, content_hash
    FROM llm_gateway_events
    WHERE gateway_id = :gateway_id AND event_id IN :event_ids
    FOR UPDATE
    """
).bindparams(sa.bindparam("event_ids", expanding=True))

_SELECT_ONE = sa.text(
    """
    SELECT content_hash
    FROM llm_gateway_events
    WHERE gateway_id = :gateway_id AND event_id = :event_id
    """
)

_UPSERT_SESSION = sa.text(
    """
    INSERT INTO llm_gateway_sessions (tenant_id, gateway_id, session_id, status)
    VALUES (:tenant_id, :gateway_id, :session_id, 'pending')
    ON CONFLICT (gateway_id, session_id) DO UPDATE
      SET updated_at = now()
    RETURNING id
    """
)

_UPSERT_CYCLE = sa.text(
    """
    INSERT INTO llm_gateway_control_cycles (
        id, tenant_id, runtime_session_id, gateway_id, session_id,
        control_generation, status, next_event_sequence
    )
    VALUES (
        :id, :tenant_id, :runtime_session_id, :gateway_id, :session_id,
        :control_generation, 'pending', 1
    )
    ON CONFLICT (gateway_id, session_id, control_generation) DO UPDATE
      SET updated_at = now()
    RETURNING id
    """
)

_INSERT_EVENT = sa.text(
    """
    INSERT INTO llm_gateway_events (
        id, tenant_id, cycle_id, gateway_id, session_id, event_id, event_type,
        control_generation, event_sequence, content_hash, event_body, trace_id, status
    )
    VALUES (
        :id, :tenant_id, :cycle_id, :gateway_id, :session_id, :event_id, :event_type,
        :control_generation, :event_sequence, :content_hash, :event_body, :trace_id, 'pending'
    )
    ON CONFLICT (gateway_id, event_id) DO NOTHING
    RETURNING event_id
    """
).bindparams(sa.bindparam("event_body", type_=JSONB))

_LOCK_CLAIM_CANDIDATE = sa.text(
    """
    SELECT
        e.id AS row_id,
        e.tenant_id,
        e.cycle_id,
        e.gateway_id,
        e.session_id,
        e.event_id,
        e.event_type,
        e.control_generation,
        e.event_sequence,
        e.event_body,
        e.content_hash,
        e.trace_id,
        e.status AS event_status,
        e.attempt_count,
        c.status AS cycle_status,
        c.next_event_sequence,
        s.id AS runtime_session_id,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_events AS e
    JOIN llm_gateway_control_cycles AS c ON c.id = e.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE (
          (
              c.status IN ('pending', 'active', 'superseded')
              AND e.event_sequence = c.next_event_sequence
          )
          OR e.event_type IN ('skill_started', 'skill_finished', 'decision_rejected')
      )
      AND e.attempt_count < :max_attempts
      AND (
          (e.status IN ('pending', 'retryable_failed') AND e.next_attempt_at <= clock_timestamp())
          OR (
              e.status = 'processing'
              AND e.lock_until IS NOT NULL
              AND e.lock_until <= clock_timestamp()
          )
      )
    ORDER BY
        e.control_generation DESC,
        CASE WHEN e.event_sequence = c.next_event_sequence THEN 0 ELSE 1 END,
        e.received_at,
        e.id
    FOR UPDATE OF s, c, e SKIP LOCKED
    LIMIT 1
    """
)

_ACTIVATE_GENERATION = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET current_generation = :control_generation,
        fence_version = fence_version + 1,
        status = 'active',
        updated_at = clock_timestamp()
    WHERE id = :runtime_session_id
    RETURNING fence_version
    """
)

_MARK_RUNTIME_ACTIVE = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET status = 'active', updated_at = clock_timestamp()
    WHERE id = :runtime_session_id AND current_generation = :control_generation
    """
)

_SUPERSEDE_OLD_CYCLES = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET status = 'superseded', updated_at = clock_timestamp()
    WHERE runtime_session_id = :runtime_session_id
      AND id <> :cycle_id
      AND control_generation < :control_generation
      AND status IN ('pending', 'active')
    """
)

_ACTIVATE_CYCLE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET status = 'active',
        started_at = COALESCE(started_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :cycle_id
    """
)

_CLAIM_EVENT = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'processing',
        attempt_count = attempt_count + 1,
        claim_token = :claim_token,
        claimed_fence_version = :claimed_fence_version,
        lock_until = clock_timestamp() + (:claim_ttl_ms * interval '1 millisecond'),
        locked_by = :worker_id,
        error_stage = NULL,
        error_category = NULL,
        started_at = COALESCE(started_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND attempt_count < :max_attempts
      AND (
          (status IN ('pending', 'retryable_failed') AND next_attempt_at <= clock_timestamp())
          OR (
              status = 'processing'
              AND lock_until IS NOT NULL
              AND lock_until <= clock_timestamp()
          )
      )
    RETURNING claim_token, claimed_fence_version, attempt_count, lock_until, locked_by
    """
)

_LOCK_CLAIM_FOR_COMPLETION = sa.text(
    """
    SELECT
        e.id AS row_id,
        e.cycle_id,
        e.event_type,
        e.event_sequence,
        e.control_generation,
        e.attempt_count,
        c.runtime_session_id,
        c.next_event_sequence,
        c.status AS cycle_status,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_events AS e
    JOIN llm_gateway_control_cycles AS c ON c.id = e.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE e.id = :row_id
      AND e.claim_token = :claim_token
      AND e.claimed_fence_version = :claimed_fence_version
      AND e.status = 'processing'
    FOR UPDATE OF s, c, e
    """
)

_COMPLETE_SUCCEEDED = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'succeeded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = NULL,
        error_category = NULL,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
      AND status = 'processing'
    RETURNING id
    """
)

_COMPLETE_RETRYABLE = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'retryable_failed',
        next_attempt_at = clock_timestamp() + (:retry_delay_ms * interval '1 millisecond'),
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = :error_stage,
        error_category = :error_category,
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
      AND status = 'processing'
    RETURNING id
    """
)

_COMPLETE_DEAD_LETTER = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'dead_letter',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = :error_stage,
        error_category = :error_category,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
      AND status = 'processing'
    RETURNING id
    """
)

_COMPLETE_MANUAL = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'manual',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = :error_stage,
        error_category = :error_category,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
      AND status = 'processing'
    RETURNING id
    """
)

_SUPERSEDE_EVENT = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'superseded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = NULL,
        error_category = NULL,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    RETURNING id
    """
)

_ADVANCE_CYCLE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET next_event_sequence = next_event_sequence + 1,
        updated_at = clock_timestamp()
    WHERE id = :cycle_id AND next_event_sequence = :event_sequence
    """
)

_SKIP_COMPLETED_CONVERGENCE_EVENTS = sa.text(
    """
    WITH RECURSIVE positions(next_event_sequence) AS (
        SELECT next_event_sequence
        FROM llm_gateway_control_cycles
        WHERE id = :cycle_id

        UNION ALL

        SELECT positions.next_event_sequence + 1
        FROM positions
        JOIN llm_gateway_events AS e
          ON e.cycle_id = :cycle_id
         AND e.event_sequence = positions.next_event_sequence
        WHERE e.event_type IN ('skill_started', 'skill_finished', 'decision_rejected')
          AND e.status IN ('succeeded', 'dead_letter', 'manual', 'superseded')
    )
    UPDATE llm_gateway_control_cycles
    SET next_event_sequence = (
            SELECT max(next_event_sequence)
            FROM positions
        ),
        updated_at = clock_timestamp()
    WHERE id = :cycle_id
    """
)

_ADVANCE_STALE_CYCLE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET next_event_sequence = next_event_sequence + 1,
        status = CASE WHEN :is_session_stopped THEN 'stopped' ELSE 'superseded' END,
        stopped_at = CASE WHEN :is_session_stopped THEN clock_timestamp() ELSE stopped_at END,
        updated_at = clock_timestamp()
    WHERE id = :cycle_id AND next_event_sequence = :event_sequence
    """
)

_STOP_CURRENT_CYCLE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET next_event_sequence = next_event_sequence + 1,
        status = 'stopped',
        stopped_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :cycle_id AND next_event_sequence = :event_sequence
    """
)

_MARK_CYCLE_MANUAL = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET status = 'manual', updated_at = clock_timestamp()
    WHERE id = :cycle_id
    """
)

_MARK_UNCLAIMED_EVENT_MANUAL = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'manual',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'generation',
        error_category = 'missing_session_started',
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    """
)

_STOP_CURRENT_RUNTIME = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET status = 'stopped', updated_at = clock_timestamp()
    WHERE id = :runtime_session_id
      AND current_generation = :control_generation
      AND fence_version = :claimed_fence_version
    """
)

_LOCK_EXHAUSTED_CLAIM = sa.text(
    """
    SELECT
        e.id AS row_id,
        e.cycle_id,
        e.event_type,
        e.event_sequence,
        e.control_generation,
        e.claim_token,
        e.claimed_fence_version,
        c.runtime_session_id,
        c.next_event_sequence,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_events AS e
    JOIN llm_gateway_control_cycles AS c ON c.id = e.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE e.status = 'processing'
      AND e.lock_until IS NOT NULL
      AND e.lock_until <= clock_timestamp()
      AND e.attempt_count >= :max_attempts
    ORDER BY e.lock_until, e.id
    FOR UPDATE OF s, c, e SKIP LOCKED
    LIMIT 1
    """
)

_RENEW_EVENT_CLAIM = sa.text(
    """
    UPDATE llm_gateway_events AS e
    SET lock_until = clock_timestamp() + (:claim_ttl_ms * interval '1 millisecond'),
        updated_at = clock_timestamp()
    FROM llm_gateway_control_cycles AS c
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE e.id = :row_id
      AND e.cycle_id = c.id
      AND e.claim_token = :claim_token
      AND e.claimed_fence_version = :claimed_fence_version
      AND e.status = 'processing'
      AND e.lock_until > clock_timestamp()
      AND (
          (
              s.current_generation = e.control_generation
              AND s.fence_version = e.claimed_fence_version
          )
          OR (
              e.control_generation < s.current_generation
              AND e.event_type IN ('skill_started', 'skill_finished', 'decision_rejected')
          )
      )
    RETURNING e.id
    """
)

_COUNT_DEAD_LETTERS = sa.text(
    """
    SELECT count(*)
    FROM llm_gateway_events
    WHERE status = 'dead_letter'
    """
)

_PERSIST_LEASE_CONTEXT = sa.text(
    """
    UPDATE llm_gateway_control_cycles AS c
    SET latest_state_version = :state_version,
        latest_decision_lease_id = :decision_lease_id,
        latest_decision_context = :decision_context,
        updated_at = clock_timestamp()
    FROM llm_gateway_events AS e, llm_gateway_sessions AS s
    WHERE c.id = :cycle_id
      AND e.id = :row_id
      AND e.cycle_id = c.id
      AND e.claim_token = :claim_token
      AND e.claimed_fence_version = :claimed_fence_version
      AND e.status = 'processing'
      AND s.id = c.runtime_session_id
      AND s.current_generation = e.control_generation
      AND s.fence_version = e.claimed_fence_version
    RETURNING c.id
    """
).bindparams(sa.bindparam("decision_context", type_=JSONB))


def _sqlstate(error: BaseException) -> str | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str):
                return value
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


def _wraps_asyncpg_data_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, AsyncpgDataError):
            return True
        seen.add(id(current))
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return False


def _is_recoverable_event_statement_error(error: BaseException) -> bool:
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return False
    sqlstate = _sqlstate(error)
    if sqlstate in {"40P01", "40001"}:
        return False
    return isinstance(error, (IntegrityError, DataError)) or _wraps_asyncpg_data_error(error)


def _prepare_batch(gateway_id: str, events: Sequence[GatewayV2Event]) -> tuple[_PreparedEvent, ...]:
    prepared_by_id: dict[str, _PreparedEvent] = {}
    for event in events:
        content_hash = event_content_hash(gateway_id, event)
        previous = prepared_by_id.get(event.event_id)
        if previous is not None:
            if previous.content_hash != content_hash:
                raise EventAdmissionConflict(event.event_id) from None
            continue
        prepared_by_id[event.event_id] = _PreparedEvent(event=event, content_hash=content_hash)
    return tuple(prepared_by_id.values())


class InboxRepository:
    def __init__(self, session_factory: _SessionFactory | Callable[[], AsyncSession] = async_session_factory) -> None:
        self._session_factory = session_factory

    async def accept_event_batch(
        self,
        identity: InboundGatewayIdentity,
        trace_id: str,
        events: Sequence[GatewayV2Event],
    ) -> BatchAcceptance:
        prepared = _prepare_batch(identity.gateway_id, events)
        received: list[str] = []
        duplicates: list[str] = []

        try:
            async with self._session_factory() as session:
                try:
                    await session.begin()
                    existing = await self._load_existing(session, identity.gateway_id, prepared)
                    for item in prepared:
                        stored_hash = existing.get(item.event.event_id)
                        if stored_hash is None:
                            continue
                        if stored_hash != item.content_hash:
                            raise EventAdmissionConflict(item.event.event_id)
                        duplicates.append(item.event.event_id)

                    for item in prepared:
                        if item.event.event_id in existing:
                            continue
                        classification = await self._insert_one(session, identity, trace_id, item)
                        if classification == "received":
                            received.append(item.event.event_id)
                        elif classification == "duplicate":
                            duplicates.append(item.event.event_id)

                    await session.commit()
                except EventAdmissionConflict:
                    await self._rollback_quietly(session)
                    raise
                except EventAdmissionUnavailable:
                    await self._rollback_quietly(session)
                    raise
                except (SQLAlchemyError, OSError, TimeoutError):
                    await self._rollback_quietly(session)
                    raise EventAdmissionUnavailable from None
        except (EventAdmissionConflict, EventAdmissionUnavailable):
            raise
        except (SQLAlchemyError, OSError, TimeoutError):
            raise EventAdmissionUnavailable from None

        return BatchAcceptance(tuple(received), tuple(duplicates))

    async def claim_next_event(
        self,
        *,
        worker_id: str,
        claim_ttl_ms: int,
        max_attempts: int,
    ) -> ClaimedGatewayEvent | None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if claim_ttl_ms <= 0:
            raise ValueError("claim_ttl_ms must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        while True:
            async with self._session_factory() as session, session.begin():
                candidate_result = await session.execute(
                    _LOCK_CLAIM_CANDIDATE,
                    {"max_attempts": max_attempts},
                )
                candidate = candidate_result.mappings().one_or_none()
                await self._after_claim_candidate_lock(candidate)
                if candidate is None:
                    return None

                disposition = classify_generation(
                    self._optional_int(candidate["current_generation"]),
                    int(candidate["control_generation"]),
                    str(candidate["event_type"]),
                    int(candidate["event_sequence"]),
                )
                if disposition is GenerationDisposition.STALE:
                    await self._supersede_locked_event(session, candidate)
                    continue
                if disposition is GenerationDisposition.WAIT:
                    await session.execute(
                        _MARK_UNCLAIMED_EVENT_MANUAL,
                        {"row_id": candidate["row_id"]},
                    )
                    await session.execute(
                        _MARK_CYCLE_MANUAL,
                        {"cycle_id": candidate["cycle_id"]},
                    )
                    continue

                fence_version = int(candidate["fence_version"])
                if disposition is GenerationDisposition.ACTIVATE_NEW:
                    fence_version = int(
                        (
                            await session.execute(
                                _ACTIVATE_GENERATION,
                                {
                                    "runtime_session_id": candidate["runtime_session_id"],
                                    "control_generation": candidate["control_generation"],
                                },
                            )
                        ).scalar_one()
                    )
                    await session.execute(
                        _SUPERSEDE_OLD_CYCLES,
                        {
                            "runtime_session_id": candidate["runtime_session_id"],
                            "cycle_id": candidate["cycle_id"],
                            "control_generation": candidate["control_generation"],
                        },
                    )
                    await session.execute(_ACTIVATE_CYCLE, {"cycle_id": candidate["cycle_id"]})
                elif (
                    disposition is GenerationDisposition.CURRENT
                    and str(candidate["cycle_status"]) == "pending"
                ):
                    await session.execute(_ACTIVATE_CYCLE, {"cycle_id": candidate["cycle_id"]})
                    await session.execute(
                        _MARK_RUNTIME_ACTIVE,
                        {
                            "runtime_session_id": candidate["runtime_session_id"],
                            "control_generation": candidate["control_generation"],
                        },
                    )

                claim_token = uuid4()
                claim_result = await session.execute(
                    _CLAIM_EVENT,
                    {
                        "row_id": candidate["row_id"],
                        "claim_token": claim_token,
                        "claimed_fence_version": fence_version,
                        "claim_ttl_ms": claim_ttl_ms,
                        "worker_id": worker_id,
                        "max_attempts": max_attempts,
                    },
                )
                claim = claim_result.mappings().one_or_none()
                if claim is None:
                    continue
                return self._build_claimed_event(
                    candidate,
                    claim,
                    historical_recovery=(
                        disposition is GenerationDisposition.HISTORICAL_RECOVERY
                    ),
                )

    async def renew_event_claim(
        self,
        event: ClaimedGatewayEvent,
        *,
        claim_ttl_ms: int,
    ) -> bool:
        if claim_ttl_ms <= 0:
            raise ValueError("claim_ttl_ms must be positive")
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                _RENEW_EVENT_CLAIM,
                {
                    "row_id": event.row_id,
                    "claim_token": event.claim_token,
                    "claimed_fence_version": event.claimed_fence_version,
                    "claim_ttl_ms": claim_ttl_ms,
                },
            )
            return result.scalar_one_or_none() is not None

    async def complete_event(
        self,
        event: ClaimedGatewayEvent,
        result: EventProcessResult,
        *,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
    ) -> bool:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base_ms <= 0 or retry_max_ms <= 0:
            raise ValueError("retry delays must be positive")
        if retry_base_ms > retry_max_ms:
            raise ValueError("retry_base_ms must not exceed retry_max_ms")

        async with self._session_factory() as session, session.begin():
            locked_result = await session.execute(
                _LOCK_CLAIM_FOR_COMPLETION,
                {
                    "row_id": event.row_id,
                    "claim_token": event.claim_token,
                    "claimed_fence_version": event.claimed_fence_version,
                },
            )
            locked = locked_result.mappings().one_or_none()
            if locked is None:
                return False

            disposition = classify_generation(
                self._optional_int(locked["current_generation"]),
                int(locked["control_generation"]),
                str(locked["event_type"]),
                int(locked["event_sequence"]),
            )
            current_fence = int(locked["fence_version"])
            claim_is_valid = (
                disposition is GenerationDisposition.HISTORICAL_RECOVERY
                or (
                    disposition is GenerationDisposition.CURRENT
                    and current_fence == event.claimed_fence_version
                )
            )
            if not claim_is_valid:
                await self._supersede_locked_event(session, locked)
                return False

            common_parameters = {
                "row_id": event.row_id,
                "claim_token": event.claim_token,
                "claimed_fence_version": event.claimed_fence_version,
                "error_stage": result.error_stage,
                "error_category": result.error_category,
            }
            if result.outcome == "succeeded":
                updated = await session.execute(_COMPLETE_SUCCEEDED, common_parameters)
                if updated.scalar_one_or_none() is None:
                    return False
                await self._complete_cycle_success(session, locked, event)
                return True

            if result.outcome == "retryable_failed":
                if int(locked["attempt_count"]) >= max_attempts:
                    updated = await session.execute(_COMPLETE_DEAD_LETTER, common_parameters)
                else:
                    attempt_count = int(locked["attempt_count"])
                    retry_delay_ms = min(retry_max_ms, retry_base_ms * 2 ** (attempt_count - 1))
                    updated = await session.execute(
                        _COMPLETE_RETRYABLE,
                        {**common_parameters, "retry_delay_ms": retry_delay_ms},
                    )
                completed = updated.scalar_one_or_none() is not None
                if completed and int(locked["attempt_count"]) >= max_attempts:
                    await self._skip_completed_convergence_events(session, locked)
                return completed

            updated = await session.execute(_COMPLETE_MANUAL, common_parameters)
            if updated.scalar_one_or_none() is None:
                return False
            if disposition is not GenerationDisposition.HISTORICAL_RECOVERY:
                await session.execute(_MARK_CYCLE_MANUAL, {"cycle_id": event.cycle_id})
            await self._skip_completed_convergence_events(session, locked)
            return True

    async def persist_lease_context(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> bool:
        if (
            context.event_id != event.event_id
            or context.session_id != event.session_id
            or context.control_generation != event.control_generation
            or context.event_sequence != event.event_sequence
        ):
            raise ValueError("decision context does not match claimed event")
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                _PERSIST_LEASE_CONTEXT,
                {
                    "state_version": context.state_version,
                    "decision_lease_id": context.decision_lease_id,
                    "decision_context": context.prompt_payload(),
                    "cycle_id": event.cycle_id,
                    "row_id": event.row_id,
                    "claim_token": event.claim_token,
                    "claimed_fence_version": event.claimed_fence_version,
                },
            )
            return result.scalar_one_or_none() is not None

    async def sweep_expired_claims(self, *, max_attempts: int) -> int:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        async with self._session_factory() as session, session.begin():
            dead_letter_count = 0
            while True:
                result = await session.execute(
                    _LOCK_EXHAUSTED_CLAIM,
                    {"max_attempts": max_attempts},
                )
                expired = result.mappings().one_or_none()
                if expired is None:
                    return dead_letter_count

                disposition = classify_generation(
                    self._optional_int(expired["current_generation"]),
                    int(expired["control_generation"]),
                    str(expired["event_type"]),
                    int(expired["event_sequence"]),
                )
                claimed_fence_version = self._optional_int(expired["claimed_fence_version"])
                claim_is_valid = (
                    disposition is GenerationDisposition.HISTORICAL_RECOVERY
                    or (
                        disposition is GenerationDisposition.CURRENT
                        and int(expired["fence_version"]) == claimed_fence_version
                    )
                )
                if not claim_is_valid:
                    await self._supersede_locked_event(session, expired)
                    continue

                completed = await session.execute(
                    _COMPLETE_DEAD_LETTER,
                    {
                        "row_id": expired["row_id"],
                        "claim_token": expired["claim_token"],
                        "claimed_fence_version": expired["claimed_fence_version"],
                        "error_stage": "worker",
                        "error_category": "claim_expired",
                    },
                )
                if completed.scalar_one_or_none() is None:
                    raise RuntimeError("expired event claim changed while locked")
                await self._skip_completed_convergence_events(session, expired)
                dead_letter_count += 1

    async def count_dead_letters(self) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(_COUNT_DEAD_LETTERS)
        return int(count or 0)

    async def _after_claim_candidate_lock(self, candidate: RowMapping | None) -> None:
        del candidate
        return None

    async def _supersede_locked_event(
        self,
        session: AsyncSession,
        row: RowMapping,
    ) -> None:
        await session.execute(_SUPERSEDE_EVENT, {"row_id": row["row_id"]})
        await session.execute(
            _ADVANCE_STALE_CYCLE,
            {
                "cycle_id": row["cycle_id"],
                "event_sequence": row["event_sequence"],
                "is_session_stopped": str(row["event_type"]) == "session_stopped",
            },
        )

    async def _complete_cycle_success(
        self,
        session: AsyncSession,
        row: RowMapping,
        event: ClaimedGatewayEvent,
    ) -> None:
        if event.event_type != "session_stopped":
            await session.execute(
                _ADVANCE_CYCLE,
                {"cycle_id": event.cycle_id, "event_sequence": event.event_sequence},
            )
            await self._skip_completed_convergence_events(session, row)
            return
        await session.execute(
            _STOP_CURRENT_CYCLE,
            {"cycle_id": event.cycle_id, "event_sequence": event.event_sequence},
        )
        await session.execute(
            _STOP_CURRENT_RUNTIME,
            {
                "runtime_session_id": row["runtime_session_id"],
                "control_generation": event.control_generation,
                "claimed_fence_version": event.claimed_fence_version,
            },
        )

    async def _skip_completed_convergence_events(
        self,
        session: AsyncSession,
        row: RowMapping,
    ) -> None:
        if str(row["event_type"]) not in {
            "skill_started",
            "skill_finished",
            "decision_rejected",
        } and int(row["next_event_sequence"]) != int(row["event_sequence"]):
            return
        await session.execute(
            _SKIP_COMPLETED_CONVERGENCE_EVENTS,
            {"cycle_id": row["cycle_id"]},
        )

    @staticmethod
    def _build_claimed_event(
        candidate: RowMapping,
        claim: RowMapping,
        *,
        historical_recovery: bool,
    ) -> ClaimedGatewayEvent:
        lock_until = claim["lock_until"]
        if not isinstance(lock_until, datetime):
            raise TypeError("database returned an invalid lock_until")
        return ClaimedGatewayEvent(
            row_id=InboxRepository._as_uuid(candidate["row_id"]),
            tenant_id=InboxRepository._as_uuid(candidate["tenant_id"]),
            cycle_id=InboxRepository._as_uuid(candidate["cycle_id"]),
            gateway_id=str(candidate["gateway_id"]),
            session_id=str(candidate["session_id"]),
            event_id=str(candidate["event_id"]),
            event_type=str(candidate["event_type"]),
            control_generation=int(candidate["control_generation"]),
            event_sequence=int(candidate["event_sequence"]),
            event=parse_gateway_v2_event(candidate["event_body"]),
            content_hash=str(candidate["content_hash"]),
            trace_id=str(candidate["trace_id"]),
            claim_token=InboxRepository._as_uuid(claim["claim_token"]),
            claimed_fence_version=int(claim["claimed_fence_version"]),
            attempt_count=int(claim["attempt_count"]),
            locked_by=str(claim["locked_by"]),
            lock_until=lock_until,
            historical_recovery=historical_recovery,
        )

    @staticmethod
    def _as_uuid(value: object) -> UUID:
        return value if isinstance(value, UUID) else UUID(str(value))

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return None if value is None else int(str(value))

    async def _load_existing(
        self,
        session: AsyncSession,
        gateway_id: str,
        prepared: tuple[_PreparedEvent, ...],
    ) -> dict[str, str]:
        if not prepared:
            return {}
        result = await session.execute(
            _SELECT_EXISTING,
            {"gateway_id": gateway_id, "event_ids": [item.event.event_id for item in prepared]},
        )
        return {str(row["event_id"]): str(row["content_hash"]) for row in result.mappings()}

    async def _insert_one(
        self,
        session: AsyncSession,
        identity: InboundGatewayIdentity,
        trace_id: str,
        item: _PreparedEvent,
    ) -> str | None:
        event = item.event
        savepoint = await session.begin_nested()
        try:
            runtime_session_id = (
                await session.execute(
                    _UPSERT_SESSION,
                    {
                        "tenant_id": identity.tenant_id,
                        "gateway_id": identity.gateway_id,
                        "session_id": event.session_id,
                    },
                )
            ).scalar_one()
            cycle_id = (
                await session.execute(
                    _UPSERT_CYCLE,
                    {
                        "id": uuid4(),
                        "tenant_id": identity.tenant_id,
                        "runtime_session_id": runtime_session_id,
                        "gateway_id": identity.gateway_id,
                        "session_id": event.session_id,
                        "control_generation": event.control_generation,
                    },
                )
            ).scalar_one()
            result = await session.execute(
                _INSERT_EVENT,
                {
                    "id": uuid4(),
                    "tenant_id": identity.tenant_id,
                    "cycle_id": cycle_id,
                    "gateway_id": identity.gateway_id,
                    "session_id": event.session_id,
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "control_generation": event.control_generation,
                    "event_sequence": event.event_sequence,
                    "content_hash": item.content_hash,
                    "event_body": event.model_dump(mode="json"),
                    "trace_id": trace_id,
                },
            )
            inserted_event_id = result.scalar_one_or_none()
            await savepoint.commit()
        except DBAPIError as error:
            if not _is_recoverable_event_statement_error(error):
                raise EventAdmissionUnavailable from None
            await savepoint.rollback()
            stored_hash = await self._load_one_hash(session, identity.gateway_id, event.event_id)
            if stored_hash is None:
                return None
            if stored_hash != item.content_hash:
                raise EventAdmissionConflict(event.event_id) from None
            return "duplicate"

        if inserted_event_id is not None:
            return "received"
        stored_hash = await self._load_one_hash(session, identity.gateway_id, event.event_id)
        if stored_hash is None:
            return None
        if stored_hash != item.content_hash:
            raise EventAdmissionConflict(event.event_id)
        return "duplicate"

    async def _load_one_hash(self, session: AsyncSession, gateway_id: str, event_id: str) -> str | None:
        result = await session.execute(
            _SELECT_ONE,
            {"gateway_id": gateway_id, "event_id": event_id},
        )
        value = result.scalar_one_or_none()
        return None if value is None else str(value)

    @staticmethod
    async def _rollback_quietly(session: AsyncSession) -> None:
        with suppress(Exception):
            await session.rollback()


InboxContentConflict = EventAdmissionConflict
InboxStoreUnavailable = EventAdmissionUnavailable
ContentConflict = EventAdmissionConflict
StoreUnavailable = EventAdmissionUnavailable
PostgresInboxRepository = InboxRepository
