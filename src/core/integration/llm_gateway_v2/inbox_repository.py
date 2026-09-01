from __future__ import annotations

import json
import time
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
from src.core.infrastructure.db import event_admission_session_factory
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.canonical import event_content_hash
from src.core.integration.llm_gateway_v2.contracts import GatewayV2Event, parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.event_worker import (
    ClaimedGatewayEvent,
    EventProcessResult,
    GenerationDisposition,
    classify_generation,
)
from src.core.integration.llm_gateway_v2.runtime_metrics import GatewayV2RuntimeMetrics, QueueMetrics
from src.core.integration.llm_gateway_v2.transaction import (
    acquire_cycle_advisory_lock,
    is_retryable_transaction_error,
    retry_database_mutation,
    try_acquire_cycle_advisory_lock,
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


_HOSTED_CHAT_EVENT_TYPES = frozenset(
    {"chat_received", "nearby_friend_chat_requested", "chat_send_result"}
)
_HOSTED_CHAT_STORAGE_SEQUENCE_FLOOR = 2**62
_BIGINT_MAX = 2**63 - 1


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

_REFRESH_SESSION_LIVENESS = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET last_event_at = clock_timestamp(), updated_at = clock_timestamp()
    WHERE gateway_id = :gateway_id
      AND session_id IN :session_ids
    """
).bindparams(sa.bindparam("session_ids", expanding=True))

_SET_ADMISSION_STATEMENT_TIMEOUT = sa.text(
    "SELECT set_config('statement_timeout', :timeout_ms, true)"
)

_UPSERT_SESSION = sa.text(
    """
    INSERT INTO llm_gateway_sessions (
        tenant_id, gateway_id, session_id, status, last_event_at
    )
    VALUES (:tenant_id, :gateway_id, :session_id, 'pending', clock_timestamp())
    ON CONFLICT (gateway_id, session_id) DO UPDATE
      SET updated_at = clock_timestamp()
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

_SELECT_HOSTED_CHAT_CYCLE = sa.text(
    """
    SELECT c.id, c.control_generation
    FROM llm_gateway_sessions AS s
    JOIN llm_gateway_control_cycles AS c ON c.runtime_session_id = s.id
    WHERE s.gateway_id = :gateway_id
      AND s.session_id = :session_id
    ORDER BY
        CASE
            WHEN s.current_generation IS NOT NULL
             AND c.control_generation = s.current_generation THEN 0
            WHEN c.status = 'active' THEN 1
            WHEN c.status = 'pending' THEN 2
            ELSE 2
        END,
        c.control_generation DESC
    FOR UPDATE OF c
    LIMIT 1
    """
)

_SELECT_HOSTED_ROLE_ID = sa.text(
    """
    SELECT c.latest_decision_context -> 'session' ->> 'RoleId'
    FROM llm_gateway_sessions AS s
    JOIN llm_gateway_control_cycles AS c ON c.runtime_session_id = s.id
    WHERE s.gateway_id = :gateway_id
      AND s.session_id = :session_id
    ORDER BY
        CASE
            WHEN s.current_generation IS NOT NULL
             AND c.control_generation = s.current_generation THEN 0
            WHEN c.status = 'active' THEN 1
            ELSE 2
        END,
        c.control_generation DESC
    LIMIT 1
    """
)

_SELECT_MAX_HOSTED_CHAT_SEQUENCE = sa.text(
    """
    SELECT max(event_sequence)
    FROM llm_gateway_events
    WHERE cycle_id = :cycle_id
      AND event_sequence >= :sequence_floor
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

_ADMIT_NON_CHAT_EVENTS = sa.text(
    """
    WITH input AS (
        SELECT *
        FROM jsonb_to_recordset(CAST(:events_json AS jsonb)) AS item(
            ordinal integer,
            event_row_id uuid,
            cycle_id uuid,
            session_id text,
            event_id text,
            event_type text,
            control_generation bigint,
            event_sequence bigint,
            content_hash text,
            event_body jsonb
        )
    ), session_input AS (
        SELECT DISTINCT ON (session_id)
            session_id,
            ordinal
        FROM input
        ORDER BY session_id, ordinal
    ), runtime_sessions AS (
        INSERT INTO llm_gateway_sessions (
            tenant_id, gateway_id, session_id, status, last_event_at
        )
        SELECT :tenant_id, :gateway_id, session_input.session_id, 'pending', clock_timestamp()
        FROM session_input
        ON CONFLICT (gateway_id, session_id) DO UPDATE
          SET updated_at = clock_timestamp()
        RETURNING id, session_id
    ), cycle_input AS (
        SELECT DISTINCT ON (session_id, control_generation)
            cycle_id,
            session_id,
            control_generation,
            ordinal
        FROM input
        ORDER BY session_id, control_generation, ordinal
    ), runtime_cycles AS (
        INSERT INTO llm_gateway_control_cycles (
            id, tenant_id, runtime_session_id, gateway_id, session_id,
            control_generation, status, next_event_sequence
        )
        SELECT
            cycle_input.cycle_id,
            :tenant_id,
            runtime_sessions.id,
            :gateway_id,
            cycle_input.session_id,
            cycle_input.control_generation,
            'pending',
            1
        FROM cycle_input
        JOIN runtime_sessions USING (session_id)
        ON CONFLICT (gateway_id, session_id, control_generation) DO UPDATE
          SET updated_at = now()
        RETURNING id, session_id, control_generation
    ), inserted_events AS (
        INSERT INTO llm_gateway_events (
            id, tenant_id, cycle_id, gateway_id, session_id, event_id, event_type,
            control_generation, event_sequence, content_hash, event_body, trace_id, status
        )
        SELECT
            input.event_row_id,
            :tenant_id,
            runtime_cycles.id,
            :gateway_id,
            input.session_id,
            input.event_id,
            input.event_type,
            input.control_generation,
            input.event_sequence,
            input.content_hash,
            input.event_body,
            :trace_id,
            'pending'
        FROM input
        JOIN runtime_cycles
          ON runtime_cycles.session_id = input.session_id
         AND runtime_cycles.control_generation = input.control_generation
        ON CONFLICT DO NOTHING
        RETURNING event_id
    )
    SELECT event_id
    FROM inserted_events
    """
)

_SELECT_CLAIM_CYCLE = sa.text(
    """
    SELECT e.cycle_id
    FROM llm_gateway_events AS e
    JOIN llm_gateway_control_cycles AS c ON c.id = e.cycle_id
    WHERE (
          (
              c.status IN ('pending', 'active', 'superseded')
              AND e.event_sequence = c.next_event_sequence
          )
          OR e.event_type IN (
              'skill_started', 'skill_finished', 'decision_rejected',
              'chat_received', 'nearby_friend_chat_requested', 'chat_send_result'
          )
          OR (e.event_type = 'session_stopped' AND c.status = 'active')
          OR (e.event_type = 'observation_updated' AND c.status = 'active')
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
        CASE
            WHEN c.status = 'pending'
             AND e.event_type IN ('observation_updated', 'session_stopped') THEN 0
            WHEN c.status = 'pending'
             AND e.event_type = 'session_started'
             AND e.event_sequence = 1 THEN 1
            WHEN c.status = 'active' THEN 2
            WHEN c.status = 'superseded' THEN 3
            ELSE 4
        END,
        e.control_generation DESC,
        CASE
            WHEN c.status <> 'active' THEN 10
            WHEN e.event_type = 'session_stopped' THEN 0
            WHEN e.event_type = 'skill_finished' THEN 1
            WHEN e.event_type = 'decision_rejected' THEN 2
            WHEN e.event_type = 'skill_started' THEN 3
            WHEN e.event_type IN (
                'chat_received', 'nearby_friend_chat_requested', 'chat_send_result'
            ) THEN 4
            WHEN e.event_type = 'observation_updated' THEN 5
            ELSE 6
        END,
        CASE WHEN e.event_type = 'observation_updated' THEN e.event_sequence END DESC NULLS LAST,
        CASE WHEN e.event_sequence = c.next_event_sequence THEN 0 ELSE 1 END,
        e.received_at,
        e.id
    LIMIT 256
    """
)

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
    WHERE e.cycle_id = :cycle_id
      AND (
          (
              c.status IN ('pending', 'active', 'superseded')
              AND e.event_sequence = c.next_event_sequence
          )
          OR e.event_type IN (
              'skill_started', 'skill_finished', 'decision_rejected',
              'chat_received', 'nearby_friend_chat_requested', 'chat_send_result'
          )
          OR (e.event_type = 'session_stopped' AND c.status = 'active')
          OR (e.event_type = 'observation_updated' AND c.status = 'active')
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
        CASE
            WHEN c.status = 'pending'
             AND e.event_type IN ('observation_updated', 'session_stopped') THEN 0
            WHEN c.status = 'pending'
             AND e.event_type = 'session_started'
             AND e.event_sequence = 1 THEN 1
            WHEN c.status = 'active' THEN 2
            WHEN c.status = 'superseded' THEN 3
            ELSE 4
        END,
        e.control_generation DESC,
        CASE
            WHEN c.status <> 'active' THEN 10
            WHEN e.event_type = 'session_stopped' THEN 0
            WHEN e.event_type = 'skill_finished' THEN 1
            WHEN e.event_type = 'decision_rejected' THEN 2
            WHEN e.event_type = 'skill_started' THEN 3
            WHEN e.event_type IN (
                'chat_received', 'nearby_friend_chat_requested', 'chat_send_result'
            ) THEN 4
            WHEN e.event_type = 'observation_updated' THEN 5
            ELSE 6
        END,
        CASE WHEN e.event_type = 'observation_updated' THEN e.event_sequence END DESC NULLS LAST,
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

_CANCEL_SUPERSEDED_DECISIONS = sa.text(
    """
    UPDATE llm_gateway_decisions AS d
    SET status = 'cancelled',
        response_status = 'cancelled',
        response_reason = 'generation_changed',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'fence',
        error_category = 'generation_changed',
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    FROM llm_gateway_control_cycles AS c
    WHERE d.cycle_id = c.id
      AND c.runtime_session_id = :runtime_session_id
      AND c.id <> :cycle_id
      AND c.control_generation < :control_generation
      AND c.status = 'superseded'
      AND d.status IN ('planned', 'sending', 'retryable_failed')
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
        WHERE e.status = 'superseded'
           OR (
               e.event_type IN ('skill_started', 'skill_finished', 'decision_rejected')
               AND e.status IN ('succeeded', 'dead_letter', 'manual')
           )
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
    SET next_event_sequence = GREATEST(next_event_sequence, :event_sequence + 1),
        status = 'stopped',
        stopped_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :cycle_id AND status IN ('pending', 'active')
    """
)

_SUPERSEDE_OPEN_CYCLE_EVENTS = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'superseded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE cycle_id = :cycle_id
      AND id <> :stop_event_id
      AND status IN ('pending', 'processing', 'retryable_failed')
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

_CLEANUP_CLOSED_CYCLE_EVENTS = sa.text(
    """
    UPDATE llm_gateway_events AS e
    SET status = CASE WHEN c.status = 'manual' THEN 'manual' ELSE 'superseded' END,
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'worker',
        error_category = 'closed_cycle_pending',
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    FROM llm_gateway_control_cycles AS c
    WHERE e.cycle_id = c.id
      AND (
          c.status IN ('stopped', 'manual')
          OR (
              c.status = 'superseded'
              AND e.event_type IN ('session_started', 'observation_updated', 'session_stopped')
          )
      )
      AND (
          e.status IN ('pending', 'retryable_failed')
          OR (c.status IN ('stopped', 'manual') AND e.status = 'processing')
      )
    RETURNING e.id
    """
)

_SELECT_STALE_EVENTS = sa.text(
    """
    SELECT
        e.id AS row_id,
        e.cycle_id,
        e.event_type,
        e.status AS event_status,
        s.id AS runtime_session_id,
        s.fence_version
    FROM llm_gateway_events AS e
    JOIN llm_gateway_control_cycles AS c ON c.id = e.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE e.status IN ('pending', 'processing', 'retryable_failed')
      AND e.event_body ? 'occurredAtMs'
      AND (
          extract(epoch FROM clock_timestamp()) * 1000
          - (e.event_body ->> 'occurredAtMs')::bigint
      ) > :max_age_ms
    ORDER BY e.received_at, e.id
    FOR UPDATE OF s, e SKIP LOCKED
    LIMIT 256
    """
)

_MARK_STALE_EVENT = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'superseded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'gateway_session',
        error_category = 'stale_event_discarded',
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND status IN ('pending', 'processing', 'retryable_failed')
    RETURNING id
    """
)

_CANCEL_STALE_DECISIONS = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'cancelled',
        response_status = 'cancelled',
        response_reason = 'stale_event_discarded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        locked_by = NULL,
        lock_until = NULL,
        error_stage = 'gateway_session',
        error_category = 'stale_event_discarded',
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE source_event_id = :row_id
      AND status IN ('planned', 'sending', 'retryable_failed')
    """
)

_CANCEL_STALE_SKILL_CALLS = sa.text(
    """
    UPDATE llm_gateway_skill_calls AS call
    SET status = 'cancelled',
        failure_category = NULL,
        reason = 'stale_event_discarded',
        retryable = false,
        completed_at = COALESCE(call.completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    FROM llm_gateway_decisions AS decision
    WHERE call.decision_row_id = decision.id
      AND decision.source_event_id = :row_id
      AND call.status IN ('pending', 'started')
    """
)

_BUMP_SESSION_FENCE = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET fence_version = fence_version + 1,
        updated_at = clock_timestamp()
    WHERE id = :runtime_session_id
      AND fence_version = :fence_version
    RETURNING fence_version
    """
)

_SELECT_IDLE_SESSIONS = sa.text(
    """
    SELECT
        session.id,
        session.current_generation,
        session.fence_version,
        (
            SELECT cycle.id
            FROM llm_gateway_control_cycles AS cycle
            WHERE cycle.runtime_session_id = session.id
              AND cycle.control_generation = session.current_generation
              AND cycle.status IN ('pending', 'active')
            LIMIT 1
        ) AS current_cycle_id
    FROM llm_gateway_sessions AS session
    WHERE session.status = 'active'
      AND session.last_event_at <= clock_timestamp() - (:idle_timeout_seconds * interval '1 second')
    ORDER BY last_event_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 256
    """
)

_STOP_IDLE_CYCLES = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET status = 'stopped',
        stopped_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE runtime_session_id = :runtime_session_id
      AND control_generation = :control_generation
      AND status IN ('pending', 'active')
    """
)

_SUPERSEDE_IDLE_EVENTS = sa.text(
    """
    UPDATE llm_gateway_events AS event
    SET status = 'superseded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'gateway_session',
        error_category = 'gateway_session_inactive',
        completed_at = COALESCE(event.completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    FROM llm_gateway_control_cycles AS cycle
    WHERE event.cycle_id = cycle.id
      AND cycle.runtime_session_id = :runtime_session_id
      AND cycle.control_generation = :control_generation
      AND event.status IN ('pending', 'processing', 'retryable_failed')
    """
)

_CANCEL_IDLE_DECISIONS = sa.text(
    """
    UPDATE llm_gateway_decisions AS decision
    SET status = 'cancelled',
        response_status = 'cancelled',
        response_reason = 'gateway_session_inactive',
        claim_token = NULL,
        claimed_fence_version = NULL,
        locked_by = NULL,
        lock_until = NULL,
        error_stage = 'gateway_session',
        error_category = 'gateway_session_inactive',
        completed_at = COALESCE(decision.completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    FROM llm_gateway_control_cycles AS cycle
    WHERE decision.cycle_id = cycle.id
      AND cycle.runtime_session_id = :runtime_session_id
      AND cycle.control_generation = :control_generation
      AND decision.status IN ('planned', 'sending', 'retryable_failed')
    """
)

_CANCEL_IDLE_SKILL_CALLS = sa.text(
    """
    UPDATE llm_gateway_skill_calls AS call
    SET status = 'cancelled',
        failure_category = NULL,
        reason = 'gateway_session_inactive',
        retryable = false,
        completed_at = COALESCE(call.completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    FROM llm_gateway_decisions AS decision
    JOIN llm_gateway_control_cycles AS cycle ON cycle.id = decision.cycle_id
    WHERE call.decision_row_id = decision.id
      AND cycle.runtime_session_id = :runtime_session_id
      AND cycle.control_generation = :control_generation
      AND call.status IN ('pending', 'started')
    """
)

_STOP_IDLE_SESSION = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET status = 'stopped',
        fence_version = fence_version + 1,
        updated_at = clock_timestamp()
    WHERE id = :runtime_session_id
      AND status = 'active'
      AND current_generation = :control_generation
      AND fence_version = :fence_version
    RETURNING id
    """
)

_STOP_CURRENT_RUNTIME = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET status = 'stopped',
        fence_version = fence_version + 1,
        updated_at = clock_timestamp()
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
        c.status AS cycle_status,
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
          e.event_type IN ('chat_received', 'nearby_friend_chat_requested', 'chat_send_result')
          OR (
              (
                  c.status IN ('pending', 'active')
                  AND s.current_generation = e.control_generation
                  AND s.fence_version = e.claimed_fence_version
                  AND (
                      c.latest_decision_lease_id IS NULL
                      OR e.event_body ->> 'decisionLeaseId' IS NULL
                      OR (
                          c.latest_decision_lease_id = e.event_body ->> 'decisionLeaseId'
                          AND c.latest_state_version = (e.event_body ->> 'stateVersion')::bigint
                      )
                  )
              )
              OR (
                  c.status = 'superseded'
                  AND e.control_generation < s.current_generation
                  AND e.event_type IN ('skill_started', 'skill_finished', 'decision_rejected')
              )
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

_SELECT_EVENT_QUEUE_METRICS = sa.text(
    """
    SELECT
        count(*) AS depth,
        COALESCE(
            extract(epoch FROM (clock_timestamp() - min(received_at))),
            0
        ) AS oldest_age_seconds
    FROM llm_gateway_events
    JOIN llm_gateway_control_cycles AS c ON c.id = llm_gateway_events.cycle_id
    WHERE c.status IN ('pending', 'active', 'superseded')
      AND (
          (
              c.status IN ('pending', 'active', 'superseded')
              AND llm_gateway_events.event_sequence = c.next_event_sequence
          )
          OR llm_gateway_events.event_type IN (
              'skill_started', 'skill_finished', 'decision_rejected',
              'chat_received', 'nearby_friend_chat_requested', 'chat_send_result'
          )
          OR (llm_gateway_events.event_type = 'session_stopped' AND c.status = 'active')
          OR (llm_gateway_events.event_type = 'observation_updated' AND c.status = 'active')
      )
      AND (
          (
              llm_gateway_events.status IN ('pending', 'retryable_failed')
              AND llm_gateway_events.next_attempt_at <= clock_timestamp()
          )
          OR (
              llm_gateway_events.status = 'processing'
              AND llm_gateway_events.lock_until IS NOT NULL
              AND llm_gateway_events.lock_until <= clock_timestamp()
          )
      )
      AND llm_gateway_events.attempt_count < :max_attempts
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

_SUPERSEDE_OLDER_LEASE_EVENTS = sa.text(
    """
    UPDATE llm_gateway_events
    SET status = 'superseded',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE cycle_id = :cycle_id
      AND id <> :row_id
      AND event_sequence < :event_sequence
      AND status IN ('pending', 'processing', 'retryable_failed')
      AND event_type IN ('session_started', 'observation_updated')
      AND event_body ->> 'decisionLeaseId' IS NOT NULL
      AND (
          event_body ->> 'decisionLeaseId' <> :decision_lease_id
          OR (event_body ->> 'stateVersion')::bigint <> :state_version
      )
    """
)

_CANCEL_OLDER_LEASE_DECISIONS = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'cancelled',
        response_status = 'cancelled',
        response_reason = CASE
            WHEN decision_lease_id <> :decision_lease_id THEN 'decision_lease_changed'
            ELSE 'state_version_changed'
        END,
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'fence',
        error_category = CASE
            WHEN decision_lease_id <> :decision_lease_id THEN 'decision_lease_changed'
            ELSE 'state_version_changed'
        END,
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE cycle_id = :cycle_id
      AND status IN ('planned', 'sending', 'retryable_failed')
      AND (
          decision_lease_id <> :decision_lease_id
          OR state_version <> :state_version
      )
    """
)


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


def _is_hosted_chat_event(event: GatewayV2Event) -> bool:
    return event.event_type in _HOSTED_CHAT_EVENT_TYPES


def _order_prepared_events_for_insertion(
    prepared: Sequence[_PreparedEvent],
) -> tuple[_PreparedEvent, ...]:
    sequenced = [item for item in prepared if not _is_hosted_chat_event(item.event)]
    hosted_chat = [item for item in prepared if _is_hosted_chat_event(item.event)]
    return tuple((*sequenced, *hosted_chat))


def _next_hosted_chat_storage_sequence(maximum: object | None) -> int:
    if maximum is None:
        return _HOSTED_CHAT_STORAGE_SEQUENCE_FLOOR
    sequence = int(maximum) + 1
    if sequence > _BIGINT_MAX:
        raise EventAdmissionUnavailable
    return sequence


def _should_mark_cycle_manual(event_type: str) -> bool:
    return event_type not in _HOSTED_CHAT_EVENT_TYPES


def _hosted_chat_cycle_is_processable(event_type: str, cycle_status: str) -> bool:
    del cycle_status
    return True


class InboxRepository:
    def __init__(
        self,
        session_factory: _SessionFactory | Callable[[], AsyncSession] = event_admission_session_factory,
        *,
        metrics: GatewayV2RuntimeMetrics | None = None,
        statement_timeout_seconds: float | None = None,
        event_stale_after_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._metrics = metrics
        if statement_timeout_seconds is None:
            from src.config import settings

            statement_timeout_seconds = settings.llm_gateway_v2_event_admission_timeout_seconds
        if statement_timeout_seconds <= 0:
            raise ValueError("statement_timeout_seconds must be positive")
        if event_stale_after_seconds is not None and event_stale_after_seconds <= 0:
            raise ValueError("event_stale_after_seconds must be positive")
        self._statement_timeout_seconds = statement_timeout_seconds
        self._event_stale_after_seconds = event_stale_after_seconds

    async def resolve_role_id(self, gateway_id: str, session_id: str) -> str | None:
        async with self._session_factory() as session:
            role_id = await session.scalar(
                _SELECT_HOSTED_ROLE_ID,
                {"gateway_id": gateway_id, "session_id": session_id},
            )
        return str(role_id) if role_id is not None else None

    @retry_database_mutation
    async def accept_event_batch(
        self,
        identity: InboundGatewayIdentity,
        trace_id: str,
        events: Sequence[GatewayV2Event],
    ) -> BatchAcceptance:
        prepared = _prepare_batch(identity.gateway_id, events)
        received: list[str] = []
        duplicates: list[str] = []
        runtime_session_cache: dict[str, object] = {}
        cycle_cache: dict[tuple[str, int], object] = {}
        hosted_cycle_cache: dict[str, tuple[object, int]] = {}

        try:
            async with self._session_factory() as session:
                try:
                    await session.begin()
                    await session.execute(
                        _SET_ADMISSION_STATEMENT_TIMEOUT,
                        {
                            "timeout_ms": str(
                                max(1, int(self._statement_timeout_seconds * 1_000))
                            )
                        },
                    )
                    existing = await self._load_existing(session, identity.gateway_id, prepared)
                    for item in prepared:
                        stored_hash = existing.get(item.event.event_id)
                        if stored_hash is None:
                            continue
                        if stored_hash != item.content_hash:
                            raise EventAdmissionConflict(item.event.event_id)
                        duplicates.append(item.event.event_id)

                    classifications: dict[str, str | None] = {}
                    pending = tuple(item for item in prepared if item.event.event_id not in existing)
                    ordered_pending = _order_prepared_events_for_insertion(pending)
                    non_chat = tuple(
                        item for item in ordered_pending if not _is_hosted_chat_event(item.event)
                    )
                    inserted_non_chat = await self._insert_non_chat_batch(
                        session,
                        identity,
                        trace_id,
                        non_chat,
                    )
                    unresolved_non_chat = tuple(
                        item for item in non_chat if item.event.event_id not in inserted_non_chat
                    )
                    stored_non_chat = await self._load_existing(
                        session,
                        identity.gateway_id,
                        unresolved_non_chat,
                    )
                    for item in non_chat:
                        event_id = item.event.event_id
                        if event_id in inserted_non_chat:
                            classifications[event_id] = "received"
                            continue
                        stored_hash = stored_non_chat.get(event_id)
                        if stored_hash is None or stored_hash != item.content_hash:
                            raise EventAdmissionConflict(event_id)
                        classifications[event_id] = "duplicate"

                    for item in ordered_pending:
                        if not _is_hosted_chat_event(item.event):
                            continue
                        classifications[item.event.event_id] = await self._insert_one(
                            session,
                            identity,
                            trace_id,
                            item,
                            runtime_session_cache=runtime_session_cache,
                            cycle_cache=cycle_cache,
                            hosted_cycle_cache=hosted_cycle_cache,
                        )

                    for item in prepared:
                        if item.event.event_id in existing:
                            continue
                        classification = classifications[item.event.event_id]
                        if classification == "received":
                            received.append(item.event.event_id)
                        elif classification == "duplicate":
                            duplicates.append(item.event.event_id)
                        else:
                            raise EventAdmissionConflict(item.event.event_id)

                    now_ms = int(time.time() * 1_000)
                    stale_after_ms = (
                        None
                        if self._event_stale_after_seconds is None
                        else int(self._event_stale_after_seconds * 1_000)
                    )
                    live_session_ids = tuple(
                        dict.fromkeys(
                            item.event.session_id
                            for item in prepared
                            if stale_after_ms is None
                            or now_ms - item.event.occurred_at_ms <= stale_after_ms
                        )
                    )
                    if live_session_ids:
                        await session.execute(
                            _REFRESH_SESSION_LIVENESS,
                            {
                                "gateway_id": identity.gateway_id,
                                "session_ids": live_session_ids,
                            },
                        )

                    await session.commit()
                except EventAdmissionConflict:
                    await self._rollback_quietly(session)
                    raise
                except EventAdmissionUnavailable:
                    await self._rollback_quietly(session)
                    raise
                except (SQLAlchemyError, OSError, TimeoutError) as error:
                    await self._rollback_quietly(session)
                    if is_retryable_transaction_error(error):
                        raise EventAdmissionUnavailable from error
                    raise EventAdmissionUnavailable from None
        except (EventAdmissionConflict, EventAdmissionUnavailable):
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            if is_retryable_transaction_error(error):
                raise EventAdmissionUnavailable from error
            raise EventAdmissionUnavailable from None

        return BatchAcceptance(tuple(received), tuple(duplicates))

    async def discard_stale_events(self, *, max_age_seconds: float | None = None) -> int:
        threshold = self._event_stale_after_seconds if max_age_seconds is None else max_age_seconds
        if threshold is None:
            return 0
        if threshold <= 0:
            raise ValueError("max_age_seconds must be positive")

        discarded = 0
        async with self._session_factory() as session, session.begin():
            while True:
                rows = (
                    await session.execute(
                        _SELECT_STALE_EVENTS,
                        {"max_age_ms": int(threshold * 1_000)},
                    )
                ).mappings().all()
                if not rows:
                    return discarded
                for row in rows:
                    await acquire_cycle_advisory_lock(session, row["cycle_id"])
                    marked = await session.execute(_MARK_STALE_EVENT, {"row_id": row["row_id"]})
                    if marked.scalar_one_or_none() is None:
                        continue
                    await session.execute(_CANCEL_STALE_DECISIONS, {"row_id": row["row_id"]})
                    await session.execute(_CANCEL_STALE_SKILL_CALLS, {"row_id": row["row_id"]})
                    if str(row["event_status"]) == "processing":
                        await session.execute(
                            _BUMP_SESSION_FENCE,
                            {
                                "runtime_session_id": row["runtime_session_id"],
                                "fence_version": row["fence_version"],
                            },
                        )
                    discarded += 1

    async def stop_idle_sessions(self, *, idle_timeout_seconds: float) -> int:
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")

        stopped = 0
        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    _SELECT_IDLE_SESSIONS,
                    {"idle_timeout_seconds": idle_timeout_seconds},
                )
            ).mappings().all()
            for row in rows:
                runtime_session_id = row["id"]
                parameters = {
                    "runtime_session_id": runtime_session_id,
                    "control_generation": row["current_generation"],
                }
                await acquire_cycle_advisory_lock(
                    session,
                    row["current_cycle_id"] or runtime_session_id,
                )
                await session.execute(_STOP_IDLE_CYCLES, parameters)
                await session.execute(_SUPERSEDE_IDLE_EVENTS, parameters)
                await session.execute(_CANCEL_IDLE_DECISIONS, parameters)
                await session.execute(_CANCEL_IDLE_SKILL_CALLS, parameters)
                stopped_row = await session.execute(
                    _STOP_IDLE_SESSION,
                    {
                        **parameters,
                        "fence_version": row["fence_version"],
                    },
                )
                if stopped_row.scalar_one_or_none() is not None:
                    stopped += 1
        return stopped

    @retry_database_mutation
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

        if self._event_stale_after_seconds is not None:
            await self.discard_stale_events()

        while True:
            async with self._session_factory() as session, session.begin():
                cycle_ids = (
                    await session.execute(
                        _SELECT_CLAIM_CYCLE,
                        {"max_attempts": max_attempts},
                    )
                ).scalars().all()
                cycle_id = None
                for candidate_cycle_id in cycle_ids:
                    if await try_acquire_cycle_advisory_lock(session, candidate_cycle_id):
                        cycle_id = candidate_cycle_id
                        break
                if cycle_id is None:
                    await self._after_claim_candidate_lock(None)
                    return None
                candidate_result = await session.execute(
                    _LOCK_CLAIM_CANDIDATE,
                    {"max_attempts": max_attempts, "cycle_id": cycle_id},
                )
                candidate = candidate_result.mappings().one_or_none()
                await self._after_claim_candidate_lock(candidate)
                if candidate is None:
                    return None

                if not _hosted_chat_cycle_is_processable(
                    str(candidate["event_type"]),
                    str(candidate["cycle_status"]),
                ):
                    await self._supersede_locked_event(session, candidate)
                    continue

                if str(candidate["event_type"]) in _HOSTED_CHAT_EVENT_TYPES:
                    disposition = GenerationDisposition.CURRENT
                else:
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
                    cancelled = await session.execute(
                        _CANCEL_SUPERSEDED_DECISIONS,
                        {
                            "runtime_session_id": candidate["runtime_session_id"],
                            "cycle_id": candidate["cycle_id"],
                            "control_generation": candidate["control_generation"],
                        },
                    )
                    if self._metrics is not None and cancelled.rowcount > 0:
                        self._metrics.record_decision_superseded(cancelled.rowcount)
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

    @retry_database_mutation
    async def renew_event_claim(
        self,
        event: ClaimedGatewayEvent,
        *,
        claim_ttl_ms: int,
    ) -> bool:
        if claim_ttl_ms <= 0:
            raise ValueError("claim_ttl_ms must be positive")
        async with self._session_factory() as session, session.begin():
            await acquire_cycle_advisory_lock(session, event.cycle_id)
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

    @retry_database_mutation
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
            await acquire_cycle_advisory_lock(session, event.cycle_id)
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

            if not _hosted_chat_cycle_is_processable(
                str(locked["event_type"]),
                str(locked["cycle_status"]),
            ):
                await self._supersede_locked_event(session, locked)
                return False

            if str(locked["event_type"]) in _HOSTED_CHAT_EVENT_TYPES:
                disposition = GenerationDisposition.CURRENT
            else:
                disposition = classify_generation(
                    self._optional_int(locked["current_generation"]),
                    int(locked["control_generation"]),
                    str(locked["event_type"]),
                    int(locked["event_sequence"]),
                )
            current_fence = int(locked["fence_version"])
            claim_is_valid = (
                event.event_type in _HOSTED_CHAT_EVENT_TYPES
                or disposition is GenerationDisposition.HISTORICAL_RECOVERY
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
            if (
                disposition is not GenerationDisposition.HISTORICAL_RECOVERY
                and _should_mark_cycle_manual(event.event_type)
            ):
                await session.execute(_MARK_CYCLE_MANUAL, {"cycle_id": event.cycle_id})
            await self._skip_completed_convergence_events(session, locked)
            return True

    @retry_database_mutation
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
            await acquire_cycle_advisory_lock(session, event.cycle_id)
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
            if result.scalar_one_or_none() is None:
                return False
            cancelled = await session.execute(
                _CANCEL_OLDER_LEASE_DECISIONS,
                {
                    "cycle_id": event.cycle_id,
                    "state_version": context.state_version,
                    "decision_lease_id": context.decision_lease_id,
                },
            )
            if self._metrics is not None and cancelled.rowcount > 0:
                self._metrics.record_decision_superseded(cancelled.rowcount)
            await session.execute(
                _SUPERSEDE_OLDER_LEASE_EVENTS,
                {
                    "cycle_id": event.cycle_id,
                    "row_id": event.row_id,
                    "event_sequence": event.event_sequence,
                    "state_version": context.state_version,
                    "decision_lease_id": context.decision_lease_id,
                },
            )
            await session.execute(
                _SKIP_COMPLETED_CONVERGENCE_EVENTS,
                {"cycle_id": event.cycle_id},
            )
            return True

    async def sweep_expired_claims(self, *, max_attempts: int) -> int:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        async with self._session_factory() as session, session.begin():
            await session.execute(_CLEANUP_CLOSED_CYCLE_EVENTS)
            dead_letter_count = 0
            while True:
                result = await session.execute(
                    _LOCK_EXHAUSTED_CLAIM,
                    {"max_attempts": max_attempts},
                )
                expired = result.mappings().one_or_none()
                if expired is None:
                    return dead_letter_count

                if not _hosted_chat_cycle_is_processable(
                    str(expired["event_type"]),
                    str(expired["cycle_status"]),
                ):
                    await self._supersede_locked_event(session, expired)
                    continue

                if str(expired["event_type"]) in _HOSTED_CHAT_EVENT_TYPES:
                    disposition = GenerationDisposition.CURRENT
                else:
                    disposition = classify_generation(
                        self._optional_int(expired["current_generation"]),
                        int(expired["control_generation"]),
                        str(expired["event_type"]),
                        int(expired["event_sequence"]),
                    )
                claimed_fence_version = self._optional_int(expired["claimed_fence_version"])
                claim_is_valid = (
                    str(expired["event_type"]) in _HOSTED_CHAT_EVENT_TYPES
                    or disposition is GenerationDisposition.HISTORICAL_RECOVERY
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

    async def queue_metrics(self, *, max_attempts: int = 5) -> QueueMetrics:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _SELECT_EVENT_QUEUE_METRICS,
                    {"max_attempts": max_attempts},
                )
            ).mappings().one()
        return QueueMetrics(
            depth=int(row["depth"] or 0),
            oldest_age_seconds=float(row["oldest_age_seconds"] or 0.0),
        )

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
        if event.event_type in _HOSTED_CHAT_EVENT_TYPES:
            return
        if event.event_type != "session_stopped":
            await session.execute(
                _ADVANCE_CYCLE,
                {"cycle_id": event.cycle_id, "event_sequence": event.event_sequence},
            )
            await self._skip_completed_convergence_events(session, row)
            return
        await session.execute(
            _SUPERSEDE_OPEN_CYCLE_EVENTS,
            {"cycle_id": event.cycle_id, "stop_event_id": event.row_id},
        )
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
        *,
        runtime_session_cache: dict[str, object],
        cycle_cache: dict[tuple[str, int], object],
        hosted_cycle_cache: dict[str, tuple[object, int]],
    ) -> str | None:
        event = item.event
        if not _is_hosted_chat_event(event):
            raise ValueError("_insert_one only accepts hosted chat events")

        savepoint = await session.begin_nested()
        created_runtime_session_key: str | None = None
        created_cycle_key: tuple[str, int] | None = None
        created_hosted_cycle_key: str | None = None
        try:
            if _is_hosted_chat_event(event):
                cached_cycle = hosted_cycle_cache.get(event.session_id)
                if cached_cycle is None:
                    cycle = (
                        await session.execute(
                            _SELECT_HOSTED_CHAT_CYCLE,
                            {
                                "gateway_id": identity.gateway_id,
                                "session_id": event.session_id,
                            },
                        )
                    ).mappings().one_or_none()
                else:
                    cycle = None
                if cached_cycle is not None:
                    cycle_id, control_generation = cached_cycle
                elif cycle is None:
                    runtime_session_id = runtime_session_cache.get(event.session_id)
                    if runtime_session_id is None:
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
                        created_runtime_session_key = event.session_id
                    control_generation = 1
                    cycle_id = (
                        await session.execute(
                            _UPSERT_CYCLE,
                            {
                                "id": uuid4(),
                                "tenant_id": identity.tenant_id,
                                "runtime_session_id": runtime_session_id,
                                "gateway_id": identity.gateway_id,
                                "session_id": event.session_id,
                                "control_generation": control_generation,
                            },
                        )
                    ).scalar_one()
                    created_hosted_cycle_key = event.session_id
                else:
                    cycle_id = cycle["id"]
                    control_generation = int(cycle["control_generation"])
                maximum = await session.scalar(
                    _SELECT_MAX_HOSTED_CHAT_SEQUENCE,
                    {
                        "cycle_id": cycle_id,
                        "sequence_floor": _HOSTED_CHAT_STORAGE_SEQUENCE_FLOOR,
                    },
                )
                event_sequence = _next_hosted_chat_storage_sequence(maximum)
            else:
                runtime_session_id = runtime_session_cache.get(event.session_id)
                if runtime_session_id is None:
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
                    created_runtime_session_key = event.session_id
                control_generation = event.control_generation
                event_sequence = event.event_sequence
                cycle_key = (event.session_id, control_generation)
                cycle_id = cycle_cache.get(cycle_key)
                if cycle_id is None:
                    cycle_id = (
                        await session.execute(
                            _UPSERT_CYCLE,
                            {
                                "id": uuid4(),
                                "tenant_id": identity.tenant_id,
                                "runtime_session_id": runtime_session_id,
                                "gateway_id": identity.gateway_id,
                                "session_id": event.session_id,
                                "control_generation": control_generation,
                            },
                        )
                    ).scalar_one()
                    created_cycle_key = cycle_key
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
                    "control_generation": control_generation,
                    "event_sequence": event_sequence,
                    "content_hash": item.content_hash,
                    "event_body": event.model_dump(mode="json"),
                    "trace_id": trace_id,
                },
            )
            inserted_event_id = result.scalar_one_or_none()
            await savepoint.commit()
            if created_runtime_session_key is not None:
                runtime_session_cache[created_runtime_session_key] = runtime_session_id
            if created_cycle_key is not None:
                cycle_cache[created_cycle_key] = cycle_id
            if created_hosted_cycle_key is not None:
                hosted_cycle_cache[created_hosted_cycle_key] = (cycle_id, control_generation)
        except DBAPIError as error:
            if not _is_recoverable_event_statement_error(error):
                raise EventAdmissionUnavailable from None
            await savepoint.rollback()
            if created_runtime_session_key is not None:
                runtime_session_cache.pop(created_runtime_session_key, None)
            if created_cycle_key is not None:
                cycle_cache.pop(created_cycle_key, None)
            if created_hosted_cycle_key is not None:
                hosted_cycle_cache.pop(created_hosted_cycle_key, None)
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

    async def _insert_non_chat_batch(
        self,
        session: AsyncSession,
        identity: InboundGatewayIdentity,
        trace_id: str,
        items: Sequence[_PreparedEvent],
    ) -> frozenset[str]:
        if not items:
            return frozenset()
        payload = [
            {
                "ordinal": ordinal,
                "event_row_id": str(uuid4()),
                "cycle_id": str(uuid4()),
                "session_id": item.event.session_id,
                "event_id": item.event.event_id,
                "event_type": item.event.event_type,
                "control_generation": item.event.control_generation,
                "event_sequence": item.event.event_sequence,
                "content_hash": item.content_hash,
                "event_body": item.event.model_dump(mode="json"),
            }
            for ordinal, item in enumerate(items)
        ]
        result = await session.execute(
            _ADMIT_NON_CHAT_EVENTS,
            {
                "tenant_id": identity.tenant_id,
                "gateway_id": identity.gateway_id,
                "trace_id": trace_id,
                "events_json": json.dumps(
                    payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            },
        )
        return frozenset(str(event_id) for event_id in result.scalars())

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
