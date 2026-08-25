from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.infrastructure.db import async_session_factory
from src.core.integration.llm_gateway_v2.activity_capacity import (
    DEFAULT_ACTIVITY_CAPACITY_POLICY,
    ActivityCapacityPolicy,
    scene_id_from_snapshot,
)
from src.core.integration.llm_gateway_v2.contracts import DecisionRejectedEvent, SessionStoppedEvent
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent
from src.core.integration.llm_gateway_v2.runtime_metrics import GatewayV2RuntimeMetrics, QueueMetrics
from src.core.integration.llm_gateway_v2.terminal_effect_service import TerminalEffectService
from src.core.integration.llm_gateway_v2.terminal_repository import MutationDisposition, MutationResult
from src.core.integration.llm_gateway_v2.transaction import (
    acquire_cycle_advisory_lock,
    is_retryable_transaction_error,
    retry_database_mutation,
)

if TYPE_CHECKING:
    from src.core.agents.gateway_v2_models import GatewayV2AgentAction, GatewayV2AgentContext
    from src.core.integration.llm_gateway_v2.activity_plan_repository import ActivityPlanBinding
    from src.core.integration.llm_gateway_v2.decision_client import DecisionClientResult


class DecisionPlanFencedError(Exception):
    def __init__(self) -> None:
        super().__init__("gateway v2 decision plan lost its claim")


class DecisionPlanConflictError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("gateway v2 decision plan conflict")


class DecisionPlanUnavailableError(Exception):
    def __init__(self) -> None:
        super().__init__("gateway v2 decision plan unavailable")


class ActivityCapacityFullError(Exception):
    def __init__(self, skill_name: str, capacity_key: str, limit: int) -> None:
        self.skill_name = skill_name
        self.capacity_key = capacity_key
        self.limit = limit
        super().__init__(f"activity capacity is full for {skill_name}")


@dataclass(frozen=True)
class PlannedDecision:
    row_id: UUID
    decision_id: str
    decision_lease_id: str
    action: str
    request_body_json: dict[str, Any]
    request_body_bytes: bytes
    body_hash: str
    action_tracking_id: UUID | None
    created: bool


@dataclass(frozen=True)
class ClaimedDecision:
    row_id: UUID
    tenant_id: UUID
    cycle_id: UUID
    gateway_id: str
    session_id: str
    decision_id: str
    decision_lease_id: str
    control_generation: int
    state_version: int
    action: str
    request_body_bytes: bytes
    body_hash: str
    claim_token: UUID
    claimed_fence_version: int
    attempt_count: int
    locked_by: str
    lock_until: datetime
    trace_id: str = ""


@dataclass(frozen=True)
class DecisionRejectionResolution:
    disposition: MutationDisposition
    status: str
    reason: str | None
    error_category: str | None = None


def resolve_decision_rejection(
    current_status: str,
    stored_reason: str | None,
    incoming_reason: str,
) -> DecisionRejectionResolution:
    if current_status in {"planned", "sending", "retryable_failed", "cancelled"}:
        return DecisionRejectionResolution(
            MutationDisposition.APPLIED,
            "rejected",
            incoming_reason,
        )
    if current_status == "rejected":
        return DecisionRejectionResolution(
            MutationDisposition.IDEMPOTENT,
            "rejected",
            stored_reason,
            None if stored_reason == incoming_reason else "rejection_reason_mismatch",
        )
    if current_status == "accepted":
        return DecisionRejectionResolution(
            MutationDisposition.CONFLICT,
            "manual",
            stored_reason,
            "rejected_after_accepted",
        )
    return DecisionRejectionResolution(
        MutationDisposition.CONFLICT,
        "manual",
        stored_reason,
        "rejection_invalid_state",
    )


def decision_rejection_identity_matches(
    decision: Mapping[str, Any],
    event: DecisionRejectedEvent,
) -> bool:
    if str(decision["session_id"]) != event.session_id:
        return False
    if int(decision["control_generation"]) != event.control_generation:
        return False
    if event.decision_lease_id is not None and str(decision["decision_lease_id"]) != event.decision_lease_id:
        return False
    if str(decision["action"]) != event.payload.action:
        return False

    body = decision["request_body_json"]
    if not isinstance(body, Mapping):
        return False
    expected_skill = body.get("skillName") if event.payload.action == "call_skill" else None
    return event.payload.skill_name == expected_skill


def session_stop_skill_status(action: str, reason: str) -> str:
    if action == "stop_hosting" and reason == "stop_hosting_requested":
        return "succeeded"
    return "cancelled"


def decision_lease_deadline_ms(
    *,
    occurred_at_ms: int,
    lease_ttl_ms: int,
    safety_window_ms: int,
) -> int:
    if occurred_at_ms < 0:
        raise ValueError("occurred_at_ms must be non-negative")
    if lease_ttl_ms <= 0:
        raise ValueError("lease_ttl_ms must be positive")
    if safety_window_ms < 0 or safety_window_ms >= lease_ttl_ms:
        raise ValueError("safety_window_ms must be non-negative and less than lease_ttl_ms")
    return occurred_at_ms + lease_ttl_ms - safety_window_ms


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_LOCK_PLAN_SOURCE = sa.text(
    """
    SELECT
        e.id AS source_event_id,
        e.status AS event_status,
        e.claim_token,
        e.claimed_fence_version,
        e.control_generation,
        c.id AS cycle_id,
        c.status AS cycle_status,
        c.latest_decision_lease_id,
        c.latest_state_version,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_events AS e
    JOIN llm_gateway_control_cycles AS c ON c.id = e.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE e.id = :source_event_id
    FOR UPDATE OF s, c, e
    """
)

_SELECT_DECISION_BY_SOURCE = sa.text(
    """
    SELECT *
    FROM llm_gateway_decisions
    WHERE source_event_id = :source_event_id
    FOR UPDATE
    """
)

_SELECT_DECISION_BY_LEASE = sa.text(
    """
    SELECT *
    FROM llm_gateway_decisions
    WHERE gateway_id = :gateway_id AND decision_lease_id = :decision_lease_id
    FOR UPDATE
    """
)

_SELECT_DECISION_BY_ID = sa.text(
    """
    SELECT *
    FROM llm_gateway_decisions
    WHERE gateway_id = :gateway_id AND decision_id = :decision_id
    FOR UPDATE
    """
)

_INSERT_ACTION_TRACKING = sa.text(
    """
    INSERT INTO action_tracking (
        tenant_id, user_id, action_type, action_desc, goal_metric,
        goal_value, baseline_value, expected_hours, deadline, status
    ) VALUES (
        :tenant_id, :user_id, :action_type, :action_desc, :goal_metric,
        :goal_value, :baseline_value, :expected_hours,
        clock_timestamp() + (:expected_hours * interval '1 hour'),
        'tracking'
    )
    RETURNING id
    """
).bindparams(sa.bindparam("expected_hours", type_=sa.Integer()))

_INSERT_PLANNED_DECISION = sa.text(
    """
    INSERT INTO llm_gateway_decisions (
        id, tenant_id, cycle_id, source_event_id, action_tracking_id,
        gateway_id, session_id, decision_id, decision_lease_id,
        control_generation, state_version, lease_expires_at_ms, action,
        request_body_json, request_body_bytes, body_hash, status,
        activity_plan_id, activity_plan_version, activity_step_id, activity_phase,
        activity_capacity_key, activity_capacity_limit, activity_capacity_expires_at
    ) VALUES (
        :id, :tenant_id, :cycle_id, :source_event_id, :action_tracking_id,
        :gateway_id, :session_id, :decision_id, :decision_lease_id,
        :control_generation, :state_version, :lease_expires_at_ms, :action,
        :request_body_json, :request_body_bytes, :body_hash, 'planned',
        :activity_plan_id, :activity_plan_version, :activity_step_id, :activity_phase,
        :activity_capacity_key, :activity_capacity_limit,
        CASE
            WHEN :activity_capacity_key IS NULL THEN NULL
            WHEN :lease_expires_at_ms IS NULL
                THEN clock_timestamp() + (:activity_capacity_ttl_seconds * interval '1 second')
            ELSE LEAST(
                clock_timestamp() + (:activity_capacity_ttl_seconds * interval '1 second'),
                to_timestamp(:lease_expires_at_ms / 1000.0)
            )
        END
    )
    RETURNING id
    """
).bindparams(
    sa.bindparam("request_body_json", type_=JSONB),
    sa.bindparam("request_body_bytes", type_=sa.LargeBinary()),
    sa.bindparam("lease_expires_at_ms", type_=sa.BigInteger()),
    sa.bindparam("activity_capacity_ttl_seconds", type_=sa.Integer()),
    sa.bindparam("activity_capacity_key", type_=sa.String(length=512)),
    sa.bindparam("activity_capacity_limit", type_=sa.Integer()),
)

_COUNT_ACTIVE_CAPACITY_RESERVATIONS = sa.text(
    """
    SELECT count(DISTINCT d.id)
    FROM llm_gateway_decisions AS d
    LEFT JOIN llm_gateway_skill_calls AS sc ON sc.decision_row_id = d.id
    WHERE d.activity_capacity_key = :activity_capacity_key
      AND d.activity_capacity_expires_at > clock_timestamp()
      AND (
          d.status IN ('planned', 'sending', 'retryable_failed')
          OR (d.status = 'accepted' AND sc.status IN ('pending', 'started'))
      )
    """
)

_LOCK_ACTIVITY_CAPACITY = sa.text(
    "SELECT pg_advisory_xact_lock(hashtextextended(:activity_capacity_key, 1))"
)

_MARK_PLAN_MANUAL = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'manual',
        error_stage = 'plan',
        error_category = :error_category,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    """
)

_SELECT_CLAIM_CYCLE = sa.text(
    """
    SELECT d.cycle_id
    FROM llm_gateway_decisions AS d
    JOIN llm_gateway_control_cycles AS c ON c.id = d.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    JOIN llm_gateway_events AS source_event ON source_event.id = d.source_event_id
    WHERE d.attempt_count < :max_attempts
      AND NOT EXISTS (
          SELECT 1
          FROM llm_gateway_decisions AS in_flight
          WHERE in_flight.cycle_id = d.cycle_id
            AND in_flight.id <> d.id
            AND in_flight.status = 'sending'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM llm_gateway_skill_calls AS active_call
          WHERE active_call.decision_row_id IN (
              SELECT active_decision.id
              FROM llm_gateway_decisions AS active_decision
              WHERE active_decision.cycle_id = d.cycle_id
          )
            AND active_call.status IN ('pending', 'started')
      )
      AND (
          (d.status IN ('planned', 'retryable_failed') AND d.next_attempt_at <= clock_timestamp())
          OR (
              d.status = 'sending'
              AND d.lock_until IS NOT NULL
              AND d.lock_until <= clock_timestamp()
          )
      )
    ORDER BY d.next_attempt_at, d.created_at, d.id
    LIMIT 1
    """
)

_LOCK_DECISION_CANDIDATE = sa.text(
    """
    SELECT
        d.id AS row_id,
        d.tenant_id,
        d.cycle_id,
        d.gateway_id,
        d.session_id,
        d.decision_id,
        d.decision_lease_id,
        d.control_generation,
        d.state_version,
        d.lease_expires_at_ms,
        (d.lease_expires_at_ms IS NULL OR d.lease_expires_at_ms > extract(epoch FROM clock_timestamp()) * 1000)
            AS lease_current,
        d.action,
        d.request_body_bytes,
        d.body_hash,
        source_event.trace_id,
        d.status AS decision_status,
        d.attempt_count,
        c.status AS cycle_status,
        c.latest_decision_lease_id,
        c.latest_state_version,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_decisions AS d
    JOIN llm_gateway_control_cycles AS c ON c.id = d.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    JOIN llm_gateway_events AS source_event ON source_event.id = d.source_event_id
    WHERE d.cycle_id = :cycle_id
      AND d.attempt_count < :max_attempts
      AND NOT EXISTS (
          SELECT 1
          FROM llm_gateway_decisions AS in_flight
          WHERE in_flight.cycle_id = d.cycle_id
            AND in_flight.id <> d.id
            AND in_flight.status = 'sending'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM llm_gateway_skill_calls AS active_call
          WHERE active_call.decision_row_id IN (
              SELECT active_decision.id
              FROM llm_gateway_decisions AS active_decision
              WHERE active_decision.cycle_id = d.cycle_id
          )
            AND active_call.status IN ('pending', 'started')
      )
      AND (
          (d.status IN ('planned', 'retryable_failed') AND d.next_attempt_at <= clock_timestamp())
          OR (
              d.status = 'sending'
              AND d.lock_until IS NOT NULL
              AND d.lock_until <= clock_timestamp()
          )
      )
    ORDER BY d.next_attempt_at, d.created_at, d.id
    FOR UPDATE OF s, c, d SKIP LOCKED
    LIMIT 1
    """
)

_CLAIM_DECISION = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'sending',
        attempt_count = attempt_count + 1,
        claim_token = :claim_token,
        claimed_fence_version = :claimed_fence_version,
        lock_until = clock_timestamp() + (:claim_ttl_ms * interval '1 millisecond'),
        locked_by = :worker_id,
        error_stage = NULL,
        error_category = NULL,
        sent_at = COALESCE(sent_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND attempt_count < :max_attempts
      AND (
          (status IN ('planned', 'retryable_failed') AND next_attempt_at <= clock_timestamp())
          OR (
              status = 'sending'
              AND lock_until IS NOT NULL
              AND lock_until <= clock_timestamp()
          )
      )
    RETURNING claim_token, claimed_fence_version, attempt_count, lock_until, locked_by
    """
)

_CANCEL_LOCKED_DECISION = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'cancelled',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = 'fence',
        error_category = :error_category,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    RETURNING id
    """
)

_LOCK_CLAIMED_DECISION = sa.text(
    """
    SELECT
        d.id AS row_id,
        d.tenant_id,
        d.cycle_id,
        d.gateway_id,
        d.session_id,
        d.decision_id,
        d.decision_lease_id,
        d.control_generation,
        d.state_version,
        d.lease_expires_at_ms,
        (d.lease_expires_at_ms IS NULL OR d.lease_expires_at_ms > extract(epoch FROM clock_timestamp()) * 1000)
            AS lease_current,
        d.action,
        d.request_body_json,
        d.action_tracking_id,
        d.status AS decision_status,
        d.attempt_count,
        source_event.trace_id,
        d.claim_token,
        d.claimed_fence_version,
        c.status AS cycle_status,
        c.latest_decision_lease_id,
        c.latest_state_version,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_decisions AS d
    JOIN llm_gateway_control_cycles AS c ON c.id = d.cycle_id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    JOIN llm_gateway_events AS source_event ON source_event.id = d.source_event_id
    WHERE d.id = :row_id
    FOR UPDATE OF s, c, d
    """
)

_RENEW_DECISION_CLAIM = sa.text(
    """
    UPDATE llm_gateway_decisions AS d
    SET lock_until = clock_timestamp() + (:claim_ttl_ms * interval '1 millisecond'),
        updated_at = clock_timestamp()
    FROM llm_gateway_control_cycles AS c, llm_gateway_sessions AS s
    WHERE d.id = :row_id
      AND d.cycle_id = c.id
      AND c.runtime_session_id = s.id
      AND d.status = 'sending'
      AND d.claim_token = :claim_token
      AND d.claimed_fence_version = :claimed_fence_version
      AND c.status = 'active'
      AND c.latest_decision_lease_id = d.decision_lease_id
      AND c.latest_state_version = d.state_version
      AND s.current_generation = d.control_generation
      AND s.fence_version = d.claimed_fence_version
      AND (d.lease_expires_at_ms IS NULL OR d.lease_expires_at_ms > extract(epoch FROM clock_timestamp()) * 1000)
    RETURNING d.id
    """
)

_COMPLETE_DECISION_RETRYABLE = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'retryable_failed',
        next_attempt_at = clock_timestamp() + (:retry_delay_ms * interval '1 millisecond'),
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = :error_stage,
        error_category = :error_category,
        response_body_json = COALESCE(:response_body_json, response_body_json),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND status = 'sending'
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
    RETURNING id
    """
).bindparams(sa.bindparam("response_body_json", type_=JSONB))

_COMPLETE_DECISION_DEAD_LETTER = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'dead_letter',
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = :error_stage,
        error_category = :error_category,
        response_body_json = COALESCE(:response_body_json, response_body_json),
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND status = 'sending'
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
    RETURNING id
    """
).bindparams(sa.bindparam("response_body_json", type_=JSONB))

_LOCK_EXHAUSTED_DECISION_CLAIM = sa.text(
    """
    SELECT d.id AS row_id, d.claim_token, d.claimed_fence_version
    FROM llm_gateway_decisions AS d
    WHERE d.status = 'sending'
      AND d.lock_until IS NOT NULL
      AND d.lock_until <= clock_timestamp()
      AND d.attempt_count >= :max_attempts
    ORDER BY d.lock_until, d.id
    FOR UPDATE OF d SKIP LOCKED
    LIMIT 1
    """
)

_COUNT_DECISION_DEAD_LETTERS = sa.text(
    """
    SELECT count(*)
    FROM llm_gateway_decisions
    WHERE status = 'dead_letter'
    """
)

_SELECT_DECISION_QUEUE_METRICS = sa.text(
    """
    SELECT
        count(*) AS depth,
        COALESCE(
            extract(epoch FROM (clock_timestamp() - min(created_at))),
            0
        ) AS oldest_age_seconds
    FROM llm_gateway_decisions
    WHERE status IN ('planned', 'retryable_failed')
      AND next_attempt_at <= clock_timestamp()
    """
)

_LOCK_RESPONSE_SKILL_CALL = sa.text(
    """
    SELECT id, decision_row_id, decision_id, skill_call_id, skill_name, status
    FROM llm_gateway_skill_calls
    WHERE gateway_id = :gateway_id AND skill_call_id = :skill_call_id
    FOR UPDATE
    """
)

_LOCK_RESPONSE_CALL_FOR_DECISION = sa.text(
    """
    SELECT id, decision_row_id, decision_id, skill_call_id, skill_name, status
    FROM llm_gateway_skill_calls
    WHERE decision_row_id = :decision_row_id
    FOR UPDATE
    """
)

_INSERT_PENDING_SKILL_CALL = sa.text(
    """
    INSERT INTO llm_gateway_skill_calls (
        tenant_id, decision_row_id, gateway_id, session_id, decision_id,
        skill_call_id, skill_name, status, effect_status
    ) VALUES (
        :tenant_id, :decision_row_id, :gateway_id, :session_id, :decision_id,
        :skill_call_id, :skill_name, 'pending', :effect_status
    )
    RETURNING id
    """
)

_MARK_RESPONSE_CALL_MANUAL = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET status = 'manual',
        failure_category = 'protocol_failed',
        reason = 'decision_response_identity_conflict',
        retryable = false,
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    """
)

_COMPLETE_DECISION_RESPONSE = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = :status,
        response_http_status = :response_http_status,
        response_status = :response_status,
        response_reason = :response_reason,
        response_body_json = :response_body_json,
        skill_call_id = :skill_call_id,
        claim_token = NULL,
        claimed_fence_version = NULL,
        lock_until = NULL,
        locked_by = NULL,
        error_stage = :error_stage,
        error_category = :error_category,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id
      AND status = 'sending'
      AND claim_token = :claim_token
      AND claimed_fence_version = :claimed_fence_version
    RETURNING id
    """
).bindparams(sa.bindparam("response_body_json", type_=JSONB))

_RECORD_DECISION_TOKEN_USAGE = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET input_tokens = :input_tokens,
        output_tokens = :output_tokens,
        total_tokens = :total_tokens,
        model_calls = :model_calls,
        usage_reported_calls = :usage_reported_calls,
        usage_missing_calls = :usage_missing_calls,
        updated_at = clock_timestamp()
    WHERE source_event_id = :source_event_id
    """
)

_LOCK_DECISION_FOR_REJECTION = sa.text(
    """
    SELECT
        id, status, response_reason, session_id, action, control_generation,
        state_version, decision_lease_id, request_body_json
    FROM llm_gateway_decisions
    WHERE gateway_id = :gateway_id AND decision_id = :decision_id
    FOR UPDATE
    """
)

_APPLY_REJECTION = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = CAST(:status AS VARCHAR(32)),
        response_status = CASE
            WHEN CAST(:status AS VARCHAR(32)) = 'rejected' THEN 'rejected'
            ELSE response_status
        END,
        response_reason = CASE
            WHEN :replace_response_reason THEN :incoming_reason
            ELSE response_reason
        END,
        error_stage = :error_stage,
        error_category = :error_category,
        completed_at = CASE
            WHEN CAST(:status AS VARCHAR(32)) IN ('rejected', 'manual') THEN clock_timestamp()
            ELSE completed_at
        END,
        updated_at = clock_timestamp()
    WHERE id = :row_id
    """
)

_LOCK_STOP_CLAIM = sa.text(
    """
    SELECT
        e.id AS row_id,
        e.cycle_id,
        e.event_sequence,
        e.control_generation,
        c.runtime_session_id,
        c.next_event_sequence,
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

_CANCEL_UNSENT_DECISIONS = sa.text(
    """
    UPDATE llm_gateway_decisions
    SET status = 'cancelled',
        response_status = 'cancelled',
        response_reason = 'session_stopped',
        claim_token = NULL,
        claimed_fence_version = NULL,
        locked_by = NULL,
        lock_until = NULL,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE cycle_id = :cycle_id
      AND status IN ('planned', 'sending', 'retryable_failed')
    """
)

_LOCK_OPEN_CALLS = sa.text(
    """
    SELECT
        call.id,
        call.effect_status,
        decision.action,
        decision.action_tracking_id
    FROM llm_gateway_skill_calls AS call
    JOIN llm_gateway_decisions AS decision ON decision.id = call.decision_row_id
    WHERE decision.cycle_id = :cycle_id
      AND call.status IN ('pending', 'started')
    ORDER BY call.created_at, call.id
    FOR UPDATE OF call
    """
)

_FINISH_STOP_CALL = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET status = :status,
        terminal_event_id = CASE WHEN :bind_terminal_event THEN :terminal_event_id ELSE terminal_event_id END,
        reason = :reason,
        retryable = false,
        effect_status = :effect_status,
        effect_applied_at = CASE WHEN :effect_status = 'applied' THEN clock_timestamp() ELSE effect_applied_at END,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id AND status IN ('pending', 'started')
    """
).bindparams(sa.bindparam("effect_status", type_=sa.String(length=24)))

_COMPLETE_STOP_EVENT = sa.text(
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

_SUPERSEDE_STOPPED_CYCLE_EVENTS = sa.text(
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

_CLOSE_CYCLE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET next_event_sequence = GREATEST(next_event_sequence, :event_sequence + 1),
        status = 'stopped',
        stopped_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :cycle_id AND status IN ('pending', 'active')
    RETURNING id
    """
)

_STOP_RUNTIME = sa.text(
    """
    UPDATE llm_gateway_sessions
    SET status = 'stopped',
        fence_version = fence_version + 1,
        updated_at = clock_timestamp()
    WHERE id = :runtime_session_id
      AND current_generation = :control_generation
      AND fence_version = :claimed_fence_version
    RETURNING id
    """
)


class OutboxRepository:
    def __init__(
        self,
        session_factory: _SessionFactory | Callable[[], AsyncSession] = async_session_factory,
        *,
        effect_service: TerminalEffectService | None = None,
        decision_id_factory: Callable[[], str] | None = None,
        lease_ttl_ms: int | None = None,
        lease_safety_window_ms: int = 0,
        metrics: GatewayV2RuntimeMetrics | None = None,
        activity_capacity_policy: ActivityCapacityPolicy = DEFAULT_ACTIVITY_CAPACITY_POLICY,
        activity_capacity_ttl_seconds: int = 1_800,
    ) -> None:
        if lease_ttl_ms is None:
            if lease_safety_window_ms != 0:
                raise ValueError("lease_safety_window_ms requires lease_ttl_ms")
        else:
            decision_lease_deadline_ms(
                occurred_at_ms=0,
                lease_ttl_ms=lease_ttl_ms,
                safety_window_ms=lease_safety_window_ms,
            )
        if activity_capacity_ttl_seconds <= 0:
            raise ValueError("activity_capacity_ttl_seconds must be positive")
        self._session_factory = session_factory
        self._effect_service = effect_service or TerminalEffectService()
        self._decision_id_factory = decision_id_factory or (lambda: str(uuid4()))
        self._lease_ttl_ms = lease_ttl_ms
        self._lease_safety_window_ms = lease_safety_window_ms
        self._metrics = metrics
        self._activity_capacity_policy = activity_capacity_policy
        self._activity_capacity_ttl_seconds = activity_capacity_ttl_seconds

    async def find_by_source_event(self, event: ClaimedGatewayEvent) -> PlannedDecision | None:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    _SELECT_DECISION_BY_SOURCE,
                    {"source_event_id": event.row_id},
                )
                row = result.mappings().one_or_none()
            return None if row is None else self._planned_from_row(row, created=False)
        except SQLAlchemyError:
            raise DecisionPlanUnavailableError from None

    async def is_event_fence_current(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> bool:
        try:
            async with self._session_factory() as session:
                await acquire_cycle_advisory_lock(session, event.cycle_id)
                result = await session.execute(
                    _LOCK_PLAN_SOURCE,
                    {"source_event_id": event.row_id},
                )
                row = result.mappings().one_or_none()
            return row is not None and self._plan_fence_matches(row, event, context)
        except SQLAlchemyError:
            raise DecisionPlanUnavailableError from None

    @retry_database_mutation
    async def record_decision_token_usage(self, event: ClaimedGatewayEvent, usage: object) -> bool:
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    _RECORD_DECISION_TOKEN_USAGE,
                    {
                        "source_event_id": event.row_id,
                        "input_tokens": getattr(usage, "input_tokens", None),
                        "output_tokens": getattr(usage, "output_tokens", None),
                        "total_tokens": getattr(usage, "total_tokens", None),
                        "model_calls": getattr(usage, "model_calls", None),
                        "usage_reported_calls": getattr(usage, "usage_reported_calls", None),
                        "usage_missing_calls": getattr(usage, "usage_missing_calls", None),
                    },
                )
                return result.rowcount > 0
        except SQLAlchemyError:
            raise DecisionPlanUnavailableError from None

    @retry_database_mutation
    async def plan_decision(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
        action: GatewayV2AgentAction,
        activity_binding: ActivityPlanBinding | None = None,
    ) -> PlannedDecision:
        from src.core.agents.gateway_v2_models import GatewayV2CallSkillAction
        from src.core.integration.llm_gateway_v2.decision_service import freeze_gateway_v2_decision

        conflict_category: str | None = None
        planned: PlannedDecision | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await acquire_cycle_advisory_lock(session, event.cycle_id)
                locked_result = await session.execute(
                    _LOCK_PLAN_SOURCE,
                    {"source_event_id": event.row_id},
                )
                locked = locked_result.mappings().one_or_none()
                if locked is None or not self._plan_fence_matches(locked, event, context):
                    raise DecisionPlanFencedError

                source_result = await session.execute(
                    _SELECT_DECISION_BY_SOURCE,
                    {"source_event_id": event.row_id},
                )
                source_decision = source_result.mappings().one_or_none()
                if source_decision is not None:
                    if self._source_decision_matches(source_decision, event, context):
                        return self._planned_from_row(source_decision, created=False)
                    await session.execute(
                        _MARK_PLAN_MANUAL,
                        {
                            "row_id": source_decision["id"],
                            "error_category": "source_event_identity_conflict",
                        },
                    )
                    conflict_category = "source_event_identity_conflict"
                else:
                    lease_result = await session.execute(
                        _SELECT_DECISION_BY_LEASE,
                        {
                            "gateway_id": event.gateway_id,
                            "decision_lease_id": context.decision_lease_id,
                        },
                    )
                    lease_decision = lease_result.mappings().one_or_none()
                    if lease_decision is not None:
                        conflict_category = "decision_lease_consumed"

                if conflict_category is None:
                    capacity_key: str | None = None
                    capacity_limit: int | None = None
                    if isinstance(action, GatewayV2CallSkillAction):
                        capacity_key = self._activity_capacity_policy.capacity_key(
                            event.gateway_id,
                            action.skill_name,
                            context.session_snapshot,
                        )
                        capacity_limit = self._activity_capacity_policy.limit_for(
                            action.skill_name,
                            scene_id=scene_id_from_snapshot(context.session_snapshot),
                        )
                        if capacity_key is not None and capacity_limit is not None:
                            await session.execute(
                                _LOCK_ACTIVITY_CAPACITY,
                                {"activity_capacity_key": capacity_key},
                            )
                            active_count = await session.scalar(
                                _COUNT_ACTIVE_CAPACITY_RESERVATIONS,
                                {"activity_capacity_key": capacity_key},
                            )
                            if int(active_count or 0) >= capacity_limit:
                                if self._metrics is not None:
                                    self._metrics.record_activity_capacity_full(
                                        action.skill_name
                                    )
                                raise ActivityCapacityFullError(
                                    action.skill_name,
                                    capacity_key,
                                    capacity_limit,
                                )
                    decision_id = self._decision_id_factory()
                    frozen = freeze_gateway_v2_decision(decision_id, event.trace_id, context, action)
                    identity_result = await session.execute(
                        _SELECT_DECISION_BY_ID,
                        {"gateway_id": event.gateway_id, "decision_id": decision_id},
                    )
                    identity_decision = identity_result.mappings().one_or_none()
                    if identity_decision is not None:
                        category = (
                            "decision_id_body_conflict"
                            if str(identity_decision["body_hash"]) != frozen.body_hash
                            else "decision_id_identity_conflict"
                        )
                        await session.execute(
                            _MARK_PLAN_MANUAL,
                            {"row_id": identity_decision["id"], "error_category": category},
                        )
                        conflict_category = category
                    else:
                        action_tracking_id: UUID | None = None
                        if isinstance(action, GatewayV2CallSkillAction):
                            metadata = action.tracking_metadata()
                            if metadata is not None:
                                action_tracking_id = self._as_uuid(
                                    (
                                        await session.execute(
                                            _INSERT_ACTION_TRACKING,
                                            {
                                                "tenant_id": event.tenant_id,
                                                "user_id": metadata.user_id,
                                                "action_type": metadata.action_type,
                                                "action_desc": action.reason,
                                                "goal_metric": metadata.goal_metric,
                                                "goal_value": metadata.goal_value,
                                                "baseline_value": metadata.baseline_value,
                                                "expected_hours": metadata.expected_hours,
                                            },
                                        )
                                    ).scalar_one()
                                )
                        row_id = uuid4()
                        inserted = await session.execute(
                            _INSERT_PLANNED_DECISION,
                            {
                                "id": row_id,
                                "tenant_id": event.tenant_id,
                                "cycle_id": event.cycle_id,
                                "source_event_id": event.row_id,
                                "action_tracking_id": action_tracking_id,
                                "gateway_id": event.gateway_id,
                                "session_id": event.session_id,
                                "decision_id": decision_id,
                                "decision_lease_id": context.decision_lease_id,
                                "control_generation": event.control_generation,
                                "state_version": context.state_version,
                                "lease_expires_at_ms": (
                                    None
                                    if self._lease_ttl_ms is None
                                    else decision_lease_deadline_ms(
                                        occurred_at_ms=event.event.occurred_at_ms,
                                        lease_ttl_ms=self._lease_ttl_ms,
                                        safety_window_ms=self._lease_safety_window_ms,
                                    )
                                ),
                                "action": action.action,
                                "request_body_json": frozen.body_json,
                                "request_body_bytes": frozen.body_bytes,
                                "body_hash": frozen.body_hash,
                                "activity_plan_id": (
                                    None if activity_binding is None else activity_binding.plan_id
                                ),
                                "activity_plan_version": (
                                    None if activity_binding is None else activity_binding.version
                                ),
                                "activity_step_id": (
                                    None if activity_binding is None else activity_binding.step_id
                                ),
                                "activity_phase": (
                                    None if activity_binding is None else activity_binding.phase
                                ),
                                "activity_capacity_key": capacity_key,
                                "activity_capacity_limit": capacity_limit,
                                "activity_capacity_ttl_seconds": self._activity_capacity_ttl_seconds,
                            },
                        )
                        if inserted.scalar_one_or_none() is None:
                            raise RuntimeError("planned decision insert returned no identity")
                        if capacity_key is not None and self._metrics is not None:
                            self._metrics.record_activity_capacity_reserved(
                                action.skill_name
                            )
                        planned = PlannedDecision(
                            row_id=row_id,
                            decision_id=decision_id,
                            decision_lease_id=context.decision_lease_id,
                            action=action.action,
                            request_body_json=frozen.body_json,
                            request_body_bytes=frozen.body_bytes,
                            body_hash=frozen.body_hash,
                            action_tracking_id=action_tracking_id,
                            created=True,
                        )
        except (
            ActivityCapacityFullError,
            DecisionPlanFencedError,
            DecisionPlanConflictError,
            DecisionPlanUnavailableError,
        ):
            raise
        except IntegrityError:
            existing = await self.find_by_source_event(event)
            if existing is not None:
                return existing
            raise DecisionPlanConflictError("decision_unique_constraint") from None
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            if is_retryable_transaction_error(error):
                raise
            raise DecisionPlanUnavailableError from None

        if conflict_category is not None:
            raise DecisionPlanConflictError(conflict_category)
        if planned is None:
            raise DecisionPlanUnavailableError
        return planned

    @retry_database_mutation
    async def claim_next_decision(
        self,
        *,
        worker_id: str,
        claim_ttl_ms: int,
        max_attempts: int,
    ) -> ClaimedDecision | None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if claim_ttl_ms <= 0 or max_attempts <= 0:
            raise ValueError("claim_ttl_ms and max_attempts must be positive")

        async with self._session_factory() as session, session.begin():
            while True:
                cycle_id = await session.scalar(
                    _SELECT_CLAIM_CYCLE,
                    {"max_attempts": max_attempts},
                )
                if cycle_id is None:
                    return None
                await acquire_cycle_advisory_lock(session, cycle_id)
                candidate_result = await session.execute(
                    _LOCK_DECISION_CANDIDATE,
                    {"max_attempts": max_attempts, "cycle_id": cycle_id},
                )
                candidate = candidate_result.mappings().one_or_none()
                if candidate is None:
                    return None
                error_category = self._decision_fence_error(candidate)
                if error_category is not None:
                    cancelled = await session.execute(
                        _CANCEL_LOCKED_DECISION,
                        {"row_id": candidate["row_id"], "error_category": error_category},
                    )
                    if self._metrics is not None and cancelled.scalar_one_or_none() is not None:
                        self._metrics.record_decision_superseded()
                    continue

                claim_token = uuid4()
                claimed = (
                    (
                        await session.execute(
                            _CLAIM_DECISION,
                            {
                                "row_id": candidate["row_id"],
                                "claim_token": claim_token,
                                "claimed_fence_version": candidate["fence_version"],
                                "claim_ttl_ms": claim_ttl_ms,
                                "worker_id": worker_id,
                                "max_attempts": max_attempts,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if claimed is None:
                    continue
                return self._claimed_decision(candidate, claimed)

    @retry_database_mutation
    async def renew_decision_claim(
        self,
        decision: ClaimedDecision,
        *,
        claim_ttl_ms: int,
    ) -> bool:
        if claim_ttl_ms <= 0:
            raise ValueError("claim_ttl_ms must be positive")
        async with self._session_factory() as session, session.begin():
            await acquire_cycle_advisory_lock(session, decision.cycle_id)
            renewed = await session.execute(
                _RENEW_DECISION_CLAIM,
                {
                    "row_id": decision.row_id,
                    "claim_token": decision.claim_token,
                    "claimed_fence_version": decision.claimed_fence_version,
                    "claim_ttl_ms": claim_ttl_ms,
                },
            )
            return renewed.scalar_one_or_none() is not None

    @retry_database_mutation
    async def complete_decision_failure(
        self,
        decision: ClaimedDecision,
        *,
        error_stage: str,
        error_category: str,
        response_body_json: Mapping[str, Any] | None = None,
        max_attempts: int,
        retry_base_ms: int,
        retry_max_ms: int,
    ) -> bool:
        if max_attempts <= 0 or retry_base_ms <= 0 or retry_max_ms <= 0:
            raise ValueError("retry configuration must be positive")
        if retry_base_ms > retry_max_ms:
            raise ValueError("retry_base_ms must not exceed retry_max_ms")

        async with self._session_factory() as session, session.begin():
            await acquire_cycle_advisory_lock(session, decision.cycle_id)
            locked = await self._lock_claimed_decision(session, decision)
            if locked is None:
                return False
            fence_error = self._decision_fence_error(locked)
            if fence_error is not None:
                cancelled = await session.execute(
                    _CANCEL_LOCKED_DECISION,
                    {"row_id": decision.row_id, "error_category": fence_error},
                )
                cancelled_id = cancelled.scalar_one_or_none()
                if self._metrics is not None and cancelled_id is not None:
                    self._metrics.record_decision_superseded()
                return cancelled_id is not None

            parameters = {
                "row_id": decision.row_id,
                "claim_token": decision.claim_token,
                "claimed_fence_version": decision.claimed_fence_version,
                "error_stage": error_stage,
                "error_category": error_category,
                "response_body_json": (
                    dict(response_body_json) if response_body_json is not None else None
                ),
            }
            if int(locked["attempt_count"]) >= max_attempts:
                completed = await session.execute(_COMPLETE_DECISION_DEAD_LETTER, parameters)
            else:
                attempt_count = int(locked["attempt_count"])
                retry_delay_ms = min(retry_max_ms, retry_base_ms * 2 ** (attempt_count - 1))
                completed = await session.execute(
                    _COMPLETE_DECISION_RETRYABLE,
                    {**parameters, "retry_delay_ms": retry_delay_ms},
                )
            return completed.scalar_one_or_none() is not None

    @retry_database_mutation
    async def record_decision_response(
        self,
        decision: ClaimedDecision,
        response: DecisionClientResult,
    ) -> bool:
        from src.core.integration.llm_gateway_v2.decision_client import DecisionClientResult

        if not isinstance(response, DecisionClientResult):
            raise TypeError("response must be DecisionClientResult")

        async with self._session_factory() as session, session.begin():
            await acquire_cycle_advisory_lock(session, decision.cycle_id)
            locked = await self._lock_claimed_decision(session, decision)
            if locked is None:
                return False
            fence_error = self._decision_fence_error(locked)
            if fence_error is not None:
                return False

            status: str = response.status
            error_stage: str | None = None
            error_category: str | None = None
            skill_call_id = response.skill_call_id
            if response.is_idempotency_conflict:
                status = "manual"
                error_stage = "http"
                error_category = "idempotency_key_conflict"
            elif response.status == "accepted" and decision.action in {"call_skill", "stop_hosting"}:
                if skill_call_id is None:
                    raise ValueError("accepted skill action must include skill_call_id")
                skill_name = self._decision_skill_name(locked)
                if skill_name is None:
                    status = "manual"
                    error_stage = "http"
                    error_category = "decision_action_invalid"
                else:
                    decision_call = (
                        (
                            await session.execute(
                                _LOCK_RESPONSE_CALL_FOR_DECISION,
                                {"decision_row_id": decision.row_id},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if decision_call is not None and str(decision_call["skill_call_id"]) != skill_call_id:
                        status = "manual"
                        error_stage = "http"
                        error_category = "skill_call_identity_conflict"
                        skill_call_id = str(decision_call["skill_call_id"])
                    else:
                        call_result = await session.execute(
                            _LOCK_RESPONSE_SKILL_CALL,
                            {
                                "gateway_id": decision.gateway_id,
                                "skill_call_id": skill_call_id,
                            },
                        )
                        call = call_result.mappings().one_or_none()
                        if call is None:
                            effect_status = "pending" if locked["action_tracking_id"] is not None else "not_applicable"
                            await session.execute(
                                _INSERT_PENDING_SKILL_CALL,
                                {
                                    "tenant_id": decision.tenant_id,
                                    "decision_row_id": decision.row_id,
                                    "gateway_id": decision.gateway_id,
                                    "session_id": decision.session_id,
                                    "decision_id": decision.decision_id,
                                    "skill_call_id": skill_call_id,
                                    "skill_name": skill_name,
                                    "effect_status": effect_status,
                                },
                            )
                        elif not self._response_call_matches(call, decision, skill_name):
                            await session.execute(
                                _MARK_RESPONSE_CALL_MANUAL,
                                {"row_id": call["id"]},
                            )
                            status = "manual"
                            error_stage = "http"
                            error_category = "skill_call_identity_conflict"
                        elif str(call["status"]) == "manual":
                            status = "manual"
                            error_stage = "http"
                            error_category = "skill_call_state_conflict"

            completed = await session.execute(
                _COMPLETE_DECISION_RESPONSE,
                {
                    "row_id": decision.row_id,
                    "claim_token": decision.claim_token,
                    "claimed_fence_version": decision.claimed_fence_version,
                    "status": status,
                    "response_http_status": response.http_status,
                    "response_status": response.status,
                    "response_reason": response.reason[:256],
                    "response_body_json": (
                        dict(response.response_body_json)
                        if response.response_body_json is not None
                        else None
                    ),
                    "skill_call_id": skill_call_id,
                    "error_stage": error_stage,
                    "error_category": error_category,
                },
            )
            return completed.scalar_one_or_none() is not None

    async def sweep_expired_decision_claims(self, *, max_attempts: int) -> int:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        async with self._session_factory() as session, session.begin():
            count = 0
            while True:
                expired = (
                    (
                        await session.execute(
                            _LOCK_EXHAUSTED_DECISION_CLAIM,
                            {"max_attempts": max_attempts},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if expired is None:
                    return count
                completed = await session.execute(
                    _COMPLETE_DECISION_DEAD_LETTER,
                    {
                        "row_id": expired["row_id"],
                        "claim_token": expired["claim_token"],
                        "claimed_fence_version": expired["claimed_fence_version"],
                        "error_stage": "worker",
                        "error_category": "claim_expired",
                        "response_body_json": None,
                    },
                )
                if completed.scalar_one_or_none() is None:
                    raise RuntimeError("expired decision claim changed while locked")
                count += 1

    async def count_decision_dead_letters(self) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(_COUNT_DECISION_DEAD_LETTERS)
        return int(count or 0)

    async def queue_metrics(self) -> QueueMetrics:
        async with self._session_factory() as session:
            row = (await session.execute(_SELECT_DECISION_QUEUE_METRICS)).mappings().one()
        return QueueMetrics(
            depth=int(row["depth"] or 0),
            oldest_age_seconds=float(row["oldest_age_seconds"] or 0.0),
        )

    @retry_database_mutation
    async def merge_decision_rejected(self, claimed: ClaimedGatewayEvent) -> MutationResult:
        event = claimed.event
        if not isinstance(event, DecisionRejectedEvent):
            raise ValueError("decision_rejected event is required")
        async with self._session_factory() as session, session.begin():
            await acquire_cycle_advisory_lock(session, claimed.cycle_id)
            result = await session.execute(
                _LOCK_DECISION_FOR_REJECTION,
                {
                    "gateway_id": claimed.gateway_id,
                    "decision_id": event.payload.decision_id,
                },
            )
            decision = result.mappings().one_or_none()
            if decision is None:
                return MutationResult(MutationDisposition.MISSING, "missing_decision")
            if not decision_rejection_identity_matches(decision, event):
                return MutationResult(MutationDisposition.CONFLICT, "decision_identity_conflict")
            resolution = resolve_decision_rejection(
                str(decision["status"]),
                None if decision["response_reason"] is None else str(decision["response_reason"]),
                event.payload.reason,
            )
            await session.execute(
                _APPLY_REJECTION,
                {
                    "row_id": decision["id"],
                    "status": resolution.status,
                    "incoming_reason": event.payload.reason,
                    "replace_response_reason": resolution.disposition is MutationDisposition.APPLIED,
                    "error_stage": "decision_rejected_event" if resolution.error_category else None,
                    "error_category": resolution.error_category,
                },
            )
            return MutationResult(resolution.disposition, resolution.error_category)

    @retry_database_mutation
    async def close_generation(self, claimed: ClaimedGatewayEvent) -> MutationResult:
        event = claimed.event
        if not isinstance(event, SessionStoppedEvent):
            raise ValueError("session_stopped event is required")
        async with self._session_factory() as session, session.begin():
            await acquire_cycle_advisory_lock(session, claimed.cycle_id)
            locked_result = await session.execute(
                _LOCK_STOP_CLAIM,
                {
                    "row_id": claimed.row_id,
                    "claim_token": claimed.claim_token,
                    "claimed_fence_version": claimed.claimed_fence_version,
                },
            )
            locked = locked_result.mappings().one_or_none()
            if locked is None or not self._is_current_claim(locked, claimed):
                return MutationResult(MutationDisposition.FENCED, "claim_lost")

            cancelled = await session.execute(_CANCEL_UNSENT_DECISIONS, {"cycle_id": claimed.cycle_id})
            if self._metrics is not None and cancelled.rowcount > 0:
                self._metrics.record_decision_superseded(cancelled.rowcount)
            calls_result = await session.execute(_LOCK_OPEN_CALLS, {"cycle_id": claimed.cycle_id})
            calls = list(calls_result.mappings())
            successful_stop_calls = [
                call
                for call in calls
                if session_stop_skill_status(str(call["action"]), event.payload.reason) == "succeeded"
            ]
            if len(successful_stop_calls) > 1:
                for call in successful_stop_calls:
                    await session.execute(
                        _FINISH_STOP_CALL,
                        {
                            "row_id": call["id"],
                            "status": "manual",
                            "bind_terminal_event": False,
                            "terminal_event_id": claimed.row_id,
                            "reason": "ambiguous_stop_hosting_call",
                            "effect_status": "manual",
                        },
                    )
                return MutationResult(MutationDisposition.CONFLICT, "ambiguous_stop_hosting_call")

            for call in calls:
                status = session_stop_skill_status(str(call["action"]), event.payload.reason)
                action_tracking_id = call["action_tracking_id"]
                effect_status = "not_applicable"
                if status == "cancelled" and action_tracking_id is not None:
                    await self._effect_service.apply(
                        session,
                        action_tracking_id=self._as_uuid(action_tracking_id),
                        terminal_status="cancelled",
                    )
                    effect_status = "applied"
                await session.execute(
                    _FINISH_STOP_CALL,
                    {
                        "row_id": call["id"],
                        "status": status,
                        "bind_terminal_event": status == "succeeded",
                        "terminal_event_id": claimed.row_id,
                        "reason": "stop_hosting_requested" if status == "succeeded" else "session_stopped",
                        "effect_status": effect_status,
                    },
                )

            completed = await session.execute(
                _COMPLETE_STOP_EVENT,
                {
                    "row_id": claimed.row_id,
                    "claim_token": claimed.claim_token,
                    "claimed_fence_version": claimed.claimed_fence_version,
                },
            )
            await session.execute(
                _SUPERSEDE_STOPPED_CYCLE_EVENTS,
                {"cycle_id": claimed.cycle_id, "stop_event_id": claimed.row_id},
            )
            closed = await session.execute(
                _CLOSE_CYCLE,
                {"cycle_id": claimed.cycle_id, "event_sequence": claimed.event_sequence},
            )
            stopped = await session.execute(
                _STOP_RUNTIME,
                {
                    "runtime_session_id": locked["runtime_session_id"],
                    "control_generation": claimed.control_generation,
                    "claimed_fence_version": claimed.claimed_fence_version,
                },
            )
            if any(result.scalar_one_or_none() is None for result in (completed, closed, stopped)):
                raise RuntimeError("session stop transaction lost its fence")
            return MutationResult(MutationDisposition.APPLIED)

    @staticmethod
    async def _lock_claimed_decision(
        session: AsyncSession,
        decision: ClaimedDecision,
    ) -> RowMapping | None:
        result = await session.execute(
            _LOCK_CLAIMED_DECISION,
            {"row_id": decision.row_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if (
            str(row["decision_status"]) != "sending"
            or str(row["claim_token"]) != str(decision.claim_token)
            or int(row["claimed_fence_version"]) != decision.claimed_fence_version
        ):
            return None
        return row

    @staticmethod
    def _decision_fence_error(row: RowMapping) -> str | None:
        if str(row["cycle_status"]) != "active":
            return "cycle_not_active"
        if int(row["current_generation"]) != int(row["control_generation"]):
            return "generation_changed"
        claimed_fence = row.get("claimed_fence_version")
        if claimed_fence is not None and int(row["fence_version"]) != int(claimed_fence):
            return "session_fence_changed"
        if str(row["latest_decision_lease_id"]) != str(row["decision_lease_id"]):
            return "decision_lease_changed"
        if int(row["latest_state_version"]) != int(row["state_version"]):
            return "state_version_changed"
        if row["lease_current"] is not True:
            return "decision_lease_expired"
        return None

    @staticmethod
    def _claimed_decision(candidate: RowMapping, claimed: RowMapping) -> ClaimedDecision:
        body = candidate["request_body_bytes"]
        lock_until = claimed["lock_until"]
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise DecisionPlanUnavailableError
        if not isinstance(lock_until, datetime):
            raise DecisionPlanUnavailableError
        return ClaimedDecision(
            row_id=OutboxRepository._as_uuid(candidate["row_id"]),
            tenant_id=OutboxRepository._as_uuid(candidate["tenant_id"]),
            cycle_id=OutboxRepository._as_uuid(candidate["cycle_id"]),
            gateway_id=str(candidate["gateway_id"]),
            session_id=str(candidate["session_id"]),
            decision_id=str(candidate["decision_id"]),
            decision_lease_id=str(candidate["decision_lease_id"]),
            control_generation=int(candidate["control_generation"]),
            state_version=int(candidate["state_version"]),
            action=str(candidate["action"]),
            request_body_bytes=bytes(body),
            body_hash=str(candidate["body_hash"]),
            claim_token=OutboxRepository._as_uuid(claimed["claim_token"]),
            claimed_fence_version=int(claimed["claimed_fence_version"]),
            attempt_count=int(claimed["attempt_count"]),
            locked_by=str(claimed["locked_by"]),
            lock_until=lock_until,
            trace_id=str(candidate["trace_id"]),
        )

    @staticmethod
    def _decision_skill_name(row: RowMapping) -> str | None:
        if str(row["action"]) == "stop_hosting":
            return "stop_hosting"
        if str(row["action"]) != "call_skill":
            return None
        body = row["request_body_json"]
        if not isinstance(body, Mapping):
            return None
        skill_name = body.get("skillName")
        return skill_name if isinstance(skill_name, str) and skill_name else None

    @staticmethod
    def _response_call_matches(
        call: RowMapping,
        decision: ClaimedDecision,
        skill_name: str,
    ) -> bool:
        return (
            str(call["decision_row_id"]) == str(decision.row_id)
            and str(call["decision_id"]) == decision.decision_id
            and str(call["skill_name"]) == skill_name
        )

    @staticmethod
    def _plan_fence_matches(
        row: RowMapping,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> bool:
        return (
            str(row["event_status"]) == "processing"
            and str(row["claim_token"]) == str(event.claim_token)
            and int(row["claimed_fence_version"]) == event.claimed_fence_version
            and int(row["control_generation"]) == event.control_generation
            and str(row["cycle_id"]) == str(event.cycle_id)
            and str(row["cycle_status"]) == "active"
            and str(row["latest_decision_lease_id"]) == context.decision_lease_id
            and int(row["latest_state_version"]) == context.state_version
            and int(row["current_generation"]) == event.control_generation
            and int(row["fence_version"]) == event.claimed_fence_version
        )

    @staticmethod
    def _source_decision_matches(
        row: RowMapping,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> bool:
        return (
            str(row["gateway_id"]) == event.gateway_id
            and str(row["cycle_id"]) == str(event.cycle_id)
            and str(row["decision_lease_id"]) == context.decision_lease_id
            and int(row["control_generation"]) == event.control_generation
            and int(row["state_version"]) == context.state_version
        )

    @staticmethod
    def _planned_from_row(row: RowMapping, *, created: bool) -> PlannedDecision:
        body_json = row["request_body_json"]
        body_bytes = row["request_body_bytes"]
        if not isinstance(body_json, Mapping):
            raise DecisionPlanUnavailableError
        if not isinstance(body_bytes, (bytes, bytearray, memoryview)):
            raise DecisionPlanUnavailableError
        action_tracking_id = row["action_tracking_id"]
        return PlannedDecision(
            row_id=OutboxRepository._as_uuid(row["id"]),
            decision_id=str(row["decision_id"]),
            decision_lease_id=str(row["decision_lease_id"]),
            action=str(row["action"]),
            request_body_json=dict(body_json),
            request_body_bytes=bytes(body_bytes),
            body_hash=str(row["body_hash"]),
            action_tracking_id=(None if action_tracking_id is None else OutboxRepository._as_uuid(action_tracking_id)),
            created=created,
        )

    @staticmethod
    def _is_current_claim(row: RowMapping, event: ClaimedGatewayEvent) -> bool:
        return (
            int(row["control_generation"]) == event.control_generation
            and int(row["current_generation"]) == event.control_generation
            and int(row["fence_version"]) == event.claimed_fence_version
        )

    @staticmethod
    def _as_uuid(value: object) -> UUID:
        return value if isinstance(value, UUID) else UUID(str(value))
