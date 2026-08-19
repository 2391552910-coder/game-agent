from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.infrastructure.db import async_session_factory
from src.core.integration.llm_gateway_v2.activity_plan import (
    ActivityPlan,
    ActivityPlanValidationError,
    complete_social_opportunity,
    record_step_started,
    record_step_terminal,
    should_retry_activity_failure,
    validate_activity_plan,
)
from src.core.integration.llm_gateway_v2.competitive_activity import is_correctable_skill_failure
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent
from src.core.integration.llm_gateway_v2.transaction import (
    acquire_cycle_advisory_lock,
    is_retryable_transaction_error,
    retry_database_mutation,
)


class ActivityPlanUnavailableError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityPlanBinding:
    plan_id: str
    version: int
    step_id: str
    phase: str


@dataclass(frozen=True)
class ActivityPlanContext:
    plan: ActivityPlan
    binding: ActivityPlanBinding
    recent_actions: tuple[Mapping[str, Any], ...]
    recent_failures: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ActivityPlanSnapshot:
    plan: ActivityPlan | None
    recent_actions: tuple[Mapping[str, Any], ...]
    recent_failures: tuple[Mapping[str, Any], ...]
    version: int = 0


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_LOCK_CYCLE_FOR_ACTIVITY = sa.text(
    """
    SELECT
        c.id,
        c.status AS cycle_status,
        c.activity_plan_id,
        c.activity_goal,
        c.activity_plan,
        c.activity_phase,
        c.activity_status,
        c.activity_current_step_id,
        c.activity_plan_version,
        c.activity_last_event_id,
        c.activity_last_event_sequence,
        e.status AS event_status,
        e.claim_token,
        e.claimed_fence_version,
        s.current_generation,
        s.fence_version
    FROM llm_gateway_control_cycles AS c
    JOIN llm_gateway_events AS e ON e.cycle_id = c.id
    JOIN llm_gateway_sessions AS s ON s.id = c.runtime_session_id
    WHERE c.id = :cycle_id AND e.id = :event_row_id
    FOR UPDATE OF c, e, s
    """
)

_SELECT_ACTIVITY_HISTORY = sa.text(
    """
    SELECT
        d.decision_id,
        d.action,
        d.request_body_json,
        d.activity_plan_id,
        d.activity_plan_version,
        d.activity_step_id,
        d.activity_phase,
        d.status AS decision_status,
        d.error_category AS decision_error_category,
        d.response_reason,
        d.created_at,
        sc.skill_call_id,
        sc.skill_name,
        sc.status AS skill_status,
        sc.failure_category,
        sc.reason AS skill_reason,
        sc.retryable,
        sc.started_at,
        sc.completed_at
    FROM llm_gateway_decisions AS d
    LEFT JOIN llm_gateway_skill_calls AS sc ON sc.decision_row_id = d.id
    WHERE d.cycle_id = :cycle_id
    ORDER BY d.created_at DESC, d.id DESC
    LIMIT 12
    """
)

_UPDATE_ACTIVITY_STATE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET activity_plan_id = :activity_plan_id,
        activity_goal = :activity_goal,
        activity_plan = :activity_plan,
        activity_phase = :activity_phase,
        activity_status = :activity_status,
        activity_current_step_id = :activity_current_step_id,
        activity_plan_version = :activity_plan_version,
        activity_last_event_id = :activity_last_event_id,
        activity_last_event_sequence = :activity_last_event_sequence,
        updated_at = clock_timestamp()
    WHERE id = :cycle_id
      AND status IN ('pending', 'active')
       AND (:last_event_sequence IS NULL OR activity_last_event_sequence IS NULL
           OR activity_last_event_sequence < :last_event_sequence
           OR (
               activity_last_event_sequence = :last_event_sequence
               AND activity_last_event_id = :activity_last_event_id
           ))
    RETURNING id
    """
).bindparams(
    sa.bindparam("activity_goal", type_=JSONB),
    sa.bindparam("activity_plan", type_=JSONB),
    sa.bindparam("last_event_sequence", type_=sa.BigInteger),
)

_UPDATE_HOSTED_CHAT_ACTIVITY_STATE = sa.text(
    """
    UPDATE llm_gateway_control_cycles
    SET activity_plan_id = :activity_plan_id,
        activity_goal = :activity_goal,
        activity_plan = :activity_plan,
        activity_phase = :activity_phase,
        activity_status = :activity_status,
        activity_current_step_id = :activity_current_step_id,
        activity_plan_version = :activity_plan_version,
        updated_at = clock_timestamp()
    WHERE id = :cycle_id
      AND status IN ('pending', 'active')
    RETURNING id
    """
).bindparams(
    sa.bindparam("activity_goal", type_=JSONB),
    sa.bindparam("activity_plan", type_=JSONB),
)

_SELECT_DECISION_BINDING = sa.text(
    """
    SELECT activity_plan_id, activity_plan_version, activity_step_id, activity_phase
    FROM llm_gateway_decisions
    WHERE cycle_id = :cycle_id AND decision_id = :decision_id
    """
)

_SELECT_ACTIVITY_CYCLE = sa.text(
    """
    SELECT
        activity_plan,
        activity_plan_version
    FROM llm_gateway_control_cycles
    WHERE id = :cycle_id
    """
)


def _history_rows(rows: list[RowMapping]) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    actions: list[Mapping[str, Any]] = []
    failures: list[Mapping[str, Any]] = []
    for row in rows:
        item = dict(row)
        actions.append(item)
        if item.get("decision_error_category") or item.get("skill_status") in {
            "failed",
            "cancelled",
            "timeout",
            "manual",
        }:
            failures.append(item)
    return tuple(actions[:12]), tuple(failures[:6])


class ActivityPlanRepository:
    def __init__(
        self,
        session_factory: _SessionFactory | Callable[[], AsyncSession] = async_session_factory,
        *,
        plan_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._plan_id_factory = plan_id_factory or (lambda: f"activity-plan-{uuid4().hex}")

    async def load(self, event: ClaimedGatewayEvent) -> ActivityPlanSnapshot:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    _SELECT_ACTIVITY_CYCLE,
                    {"cycle_id": event.cycle_id},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise ActivityPlanUnavailableError
                history_result = await session.execute(
                    _SELECT_ACTIVITY_HISTORY,
                    {"cycle_id": event.cycle_id},
                )
                actions, failures = _history_rows(list(history_result.mappings().all()))
                plan = self._load_plan(row)
                return ActivityPlanSnapshot(
                    plan=plan,
                    version=int(row["activity_plan_version"] or 0),
                    recent_actions=actions,
                    recent_failures=failures,
                )
        except ActivityPlanUnavailableError:
            raise
        except (ActivityPlanValidationError, SQLAlchemyError, OSError) as error:
            if is_retryable_transaction_error(error):
                raise
            logger.error(
                "Activity plan load failed",
                extra={"error_type": type(error).__name__},
                exc_info=True,
            )
            raise ActivityPlanUnavailableError from error

    @retry_database_mutation
    async def prepare(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
        *,
        proposed_plan: ActivityPlan | None = None,
    ) -> ActivityPlanContext:
        try:
            async with self._session_factory() as session, session.begin():
                await acquire_cycle_advisory_lock(session, event.cycle_id)
                row = await self._lock_cycle(session, event)
                self._validate_claim(row, event)
                plan = self._load_plan(row)
                replace_existing = (
                    proposed_plan is not None
                    and (
                        plan is None
                        or proposed_plan.plan_id != plan.plan_id
                        or proposed_plan.version > plan.version
                    )
                )
                if replace_existing or plan is None or plan.status != "active":
                    if proposed_plan is None:
                        raise ActivityPlanUnavailableError
                    version = int(row["activity_plan_version"] or 0) + 1
                    plan = proposed_plan
                    if plan.version != version:
                        plan = plan.model_copy(update={"version": version})
                    plan = validate_activity_plan(plan.model_dump(mode="json", by_alias=True))
                    await self._write_state(session, event, plan)
                elif int(row["activity_last_event_sequence"] or 0) < event.event_sequence:
                    await self._write_last_event(session, event, plan)
                history_result = await session.execute(
                    _SELECT_ACTIVITY_HISTORY,
                    {"cycle_id": event.cycle_id},
                )
                actions, failures = _history_rows(list(history_result.mappings().all()))
                return ActivityPlanContext(
                    plan=plan,
                    binding=self._binding(plan),
                    recent_actions=actions,
                    recent_failures=failures,
                )
        except ActivityPlanUnavailableError:
            raise
        except (ActivityPlanValidationError, SQLAlchemyError, OSError) as error:
            if is_retryable_transaction_error(error):
                raise
            logger.error(
                "Activity plan prepare failed",
                extra={"error_type": type(error).__name__},
                exc_info=True,
            )
            raise ActivityPlanUnavailableError from error

    async def record_skill_started(self, event: ClaimedGatewayEvent) -> bool:
        return await self._record_terminal_event(event, succeeded=None, retryable=False)

    async def record_skill_finished(self, event: ClaimedGatewayEvent) -> bool:
        payload = event.event.payload
        status = getattr(payload, "status", None)
        succeeded = status == "success"
        retryable = bool(getattr(payload, "retryable", False))
        skill_name = str(getattr(payload, "skill_name", ""))
        reason = str(getattr(payload, "reason", ""))
        plan_retryable = should_retry_activity_failure(
            skill_name,
            reason,
            retryable=retryable,
        )
        corrected_decision_allowed = (
            not succeeded
            and getattr(payload, "lease", None) is not None
            and is_correctable_skill_failure(
                skill_name,
                reason,
            )
        )
        return await self._record_terminal_event(
            event,
            succeeded=succeeded,
            retryable=plan_retryable,
            corrected_decision_allowed=corrected_decision_allowed,
        )

    async def record_decision_rejected(self, event: ClaimedGatewayEvent) -> bool:
        payload = event.event.payload
        decision_id = str(getattr(payload, "decision_id", ""))
        if not decision_id:
            return True
        return await self._record_terminal_event(
            event,
            succeeded=False,
            retryable=False,
            decision_id=decision_id,
        )

    async def record_observation(self, event: ClaimedGatewayEvent) -> bool:
        return await self._complete_passive_step(event)

    async def record_chat_opportunity(self, event: ClaimedGatewayEvent) -> bool:
        return await self._complete_passive_step(event, track_event_order=False)

    async def complete_passive_step(self, event: ClaimedGatewayEvent) -> bool:
        return await self._complete_passive_step(event)

    @retry_database_mutation
    async def _complete_passive_step(
        self,
        event: ClaimedGatewayEvent,
        *,
        track_event_order: bool = True,
    ) -> bool:
        try:
            async with self._session_factory() as session, session.begin():
                await acquire_cycle_advisory_lock(session, event.cycle_id)
                row = await self._lock_cycle(session, event)
                self._validate_claim(row, event)
                plan = self._load_plan(row)
                if plan is None or plan.status != "active" or plan.current_step_id is None:
                    return True
                current = plan.current_step()
                if current.phase != "social" or current.skill_name is not None:
                    return True
                updated = complete_social_opportunity(plan)
                if track_event_order:
                    await self._write_state(session, event, updated)
                else:
                    await self._write_hosted_chat_state(session, event, updated)
                return True
        except (ActivityPlanValidationError, SQLAlchemyError, OSError) as error:
            if is_retryable_transaction_error(error):
                raise
            raise ActivityPlanUnavailableError from error

    @retry_database_mutation
    async def close(self, event: ClaimedGatewayEvent) -> bool:
        try:
            async with self._session_factory() as session, session.begin():
                await acquire_cycle_advisory_lock(session, event.cycle_id)
                row = await self._lock_cycle(session, event)
                self._validate_claim(row, event)
                plan = self._load_plan(row)
                if plan is None:
                    return True
                updated = plan.model_copy(update={"status": "paused"})
                await self._write_state(session, event, updated)
                return True
        except (ActivityPlanValidationError, SQLAlchemyError, OSError) as error:
            if is_retryable_transaction_error(error):
                raise
            raise ActivityPlanUnavailableError from error

    @retry_database_mutation
    async def _record_terminal_event(
        self,
        event: ClaimedGatewayEvent,
        *,
        succeeded: bool | None,
        retryable: bool,
        corrected_decision_allowed: bool = False,
        decision_id: str | None = None,
    ) -> bool:
        try:
            async with self._session_factory() as session, session.begin():
                await acquire_cycle_advisory_lock(session, event.cycle_id)
                row = await self._lock_cycle(session, event)
                self._validate_claim(row, event)
                plan = self._load_plan(row)
                if plan is None or plan.status != "active":
                    return True
                payload = event.event.payload
                resolved_decision_id = decision_id or str(getattr(payload, "decision_id", ""))
                binding_result = await session.execute(
                    _SELECT_DECISION_BINDING,
                    {"cycle_id": event.cycle_id, "decision_id": resolved_decision_id},
                )
                binding = binding_result.mappings().one_or_none()
                if binding is None or str(binding["activity_plan_id"] or "") != plan.plan_id:
                    return True
                step_id = str(binding["activity_step_id"] or "")
                if plan.current_step_id != step_id:
                    return True
                if succeeded is None:
                    updated = record_step_started(plan, step_id)
                else:
                    updated = record_step_terminal(
                        plan,
                        step_id,
                        succeeded=succeeded,
                        retryable=retryable,
                        corrected_decision_allowed=corrected_decision_allowed,
                    )
                await self._write_state(session, event, updated)
                return True
        except (ActivityPlanValidationError, SQLAlchemyError, OSError) as error:
            if is_retryable_transaction_error(error):
                raise
            raise ActivityPlanUnavailableError from error

    @staticmethod
    async def _lock_cycle(session: AsyncSession, event: ClaimedGatewayEvent) -> RowMapping:
        result = await session.execute(
            _LOCK_CYCLE_FOR_ACTIVITY,
            {"cycle_id": event.cycle_id, "event_row_id": event.row_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ActivityPlanUnavailableError
        return row

    @staticmethod
    def _validate_claim(row: RowMapping, event: ClaimedGatewayEvent) -> None:
        if (
            str(row["event_status"]) != "processing"
            or str(row["claim_token"]) != str(event.claim_token)
            or int(row["claimed_fence_version"]) != event.claimed_fence_version
            or int(row["current_generation"]) != event.control_generation
            or int(row["fence_version"]) != event.claimed_fence_version
        ):
            raise ActivityPlanUnavailableError

    @staticmethod
    def _load_plan(row: RowMapping) -> ActivityPlan | None:
        raw = row["activity_plan"]
        if raw is None:
            return None
        return validate_activity_plan(raw)

    async def _write_state(
        self,
        session: AsyncSession,
        event: ClaimedGatewayEvent,
        plan: ActivityPlan,
    ) -> None:
        parameters = self._state_parameters(event, plan)
        result = await session.execute(_UPDATE_ACTIVITY_STATE, parameters)
        if result.scalar_one_or_none() is None:
            current_result = await session.execute(
                sa.text(
                    "SELECT activity_last_event_sequence FROM llm_gateway_control_cycles WHERE id=:cycle_id"
                ),
                {"cycle_id": event.cycle_id},
            )
            current_sequence = current_result.scalar_one_or_none()
            if current_sequence is None or int(current_sequence) < event.event_sequence:
                raise ActivityPlanUnavailableError

    async def _write_last_event(
        self,
        session: AsyncSession,
        event: ClaimedGatewayEvent,
        plan: ActivityPlan,
    ) -> None:
        await self._write_state(session, event, plan)

    async def _write_hosted_chat_state(
        self,
        session: AsyncSession,
        event: ClaimedGatewayEvent,
        plan: ActivityPlan,
    ) -> None:
        parameters = self._hosted_chat_state_parameters(event, plan)
        result = await session.execute(_UPDATE_HOSTED_CHAT_ACTIVITY_STATE, parameters)
        if result.scalar_one_or_none() is None:
            raise ActivityPlanUnavailableError

    @staticmethod
    def _state_parameters(event: ClaimedGatewayEvent, plan: ActivityPlan) -> dict[str, Any]:
        return {
            **ActivityPlanRepository._hosted_chat_state_parameters(event, plan),
            "activity_last_event_id": event.row_id,
            "activity_last_event_sequence": event.event_sequence,
            "last_event_sequence": event.event_sequence,
        }

    @staticmethod
    def _hosted_chat_state_parameters(event: ClaimedGatewayEvent, plan: ActivityPlan) -> dict[str, Any]:
        body = plan.model_dump(mode="json", by_alias=True)
        goal = {
            "goalId": plan.goal_id,
            "goalSummary": plan.goal_summary,
        }
        return {
            "cycle_id": event.cycle_id,
            "activity_plan_id": plan.plan_id,
            "activity_goal": goal,
            "activity_plan": body,
            "activity_phase": plan.phase,
            "activity_status": plan.status,
            "activity_current_step_id": plan.current_step_id,
            "activity_plan_version": plan.version,
        }

    @staticmethod
    def _binding(plan: ActivityPlan) -> ActivityPlanBinding:
        current = plan.current_step()
        return ActivityPlanBinding(plan.plan_id, plan.version, current.step_id, current.phase)
