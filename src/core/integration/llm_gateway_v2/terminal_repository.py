from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.infrastructure.db import async_session_factory
from src.core.integration.llm_gateway_v2.contracts import (
    SkillFinishedEvent,
    SkillStartedEvent,
    SkillTerminal,
)
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent
from src.core.integration.llm_gateway_v2.terminal_effect_service import TerminalEffectService


class MutationDisposition(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    MISSING = "missing"
    FENCED = "fenced"


@dataclass(frozen=True)
class MutationResult:
    disposition: MutationDisposition
    error_category: str | None = None


@dataclass(frozen=True)
class TerminalRecord:
    status: str
    failure_category: str | None = None
    reason: str | None = None
    retryable: bool | None = None
    terminal_event_id: str | None = None

    @classmethod
    def pending(cls) -> TerminalRecord:
        return cls(status="pending")

    def with_terminal_event_id(self, event_id: str) -> TerminalRecord:
        return replace(self, terminal_event_id=event_id)

    def logical_result(self) -> tuple[str, str | None, str | None, bool | None]:
        return self.status, self.failure_category, self.reason, self.retryable


@dataclass(frozen=True)
class TerminalTransition:
    disposition: MutationDisposition
    record: TerminalRecord


_UNCONFIRMED_COMPLETION_REASONS = {
    "completion_unconfirmed",
    "vehicle_completion_unconfirmed",
}


def normalize_skill_terminal(terminal: SkillTerminal) -> TerminalRecord:
    if terminal.status == "success":
        return TerminalRecord(status="succeeded")

    reason = terminal.reason
    retryable = terminal.retryable and reason not in _UNCONFIRMED_COMPLETION_REASONS
    if terminal.status == "failed":
        return TerminalRecord(
            status="failed",
            failure_category=terminal.failure_category,
            reason=reason,
            retryable=retryable,
        )
    return TerminalRecord(
        status=terminal.status,
        reason=reason,
        retryable=retryable,
    )


def resolve_terminal_transition(
    existing: TerminalRecord,
    incoming: TerminalRecord,
) -> TerminalTransition:
    if existing.status in {"pending", "started"}:
        return TerminalTransition(MutationDisposition.APPLIED, incoming)
    if existing.logical_result() == incoming.logical_result():
        return TerminalTransition(MutationDisposition.IDEMPOTENT, existing)
    if existing.status == "manual":
        return TerminalTransition(MutationDisposition.CONFLICT, existing)
    return TerminalTransition(
        MutationDisposition.CONFLICT,
        TerminalRecord(
            status="manual",
            failure_category="internal_failed",
            reason="terminal_conflict",
            retryable=False,
            terminal_event_id=existing.terminal_event_id,
        ),
    )


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_LOCK_DECISION = sa.text(
    """
    SELECT id, decision_id, action, request_body_json, action_tracking_id
    FROM llm_gateway_decisions
    WHERE gateway_id = :gateway_id AND decision_id = :decision_id
    FOR UPDATE
    """
)

_LOCK_SKILL_CALL = sa.text(
    """
    SELECT
        id, decision_row_id, decision_id, status, failure_category,
        reason, retryable, terminal_event_id, effect_status
    FROM llm_gateway_skill_calls
    WHERE gateway_id = :gateway_id AND skill_call_id = :skill_call_id
    FOR UPDATE
    """
)

_INSERT_STARTED = sa.text(
    """
    INSERT INTO llm_gateway_skill_calls (
        tenant_id, decision_row_id, gateway_id, session_id, decision_id,
        skill_call_id, skill_name, status, started_at
    )
    VALUES (
        :tenant_id, :decision_row_id, :gateway_id, :session_id, :decision_id,
        :skill_call_id, :skill_name, 'started', clock_timestamp()
    )
    RETURNING id
    """
)

_MARK_STARTED = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET status = 'started',
        started_at = COALESCE(started_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id AND status = 'pending'
    RETURNING id
    """
)

_INSERT_TERMINAL = sa.text(
    """
    INSERT INTO llm_gateway_skill_calls (
        tenant_id, decision_row_id, terminal_event_id, gateway_id, session_id,
        decision_id, skill_call_id, skill_name, status, failure_category,
        reason, retryable, effect_status, completed_at
    )
    VALUES (
        :tenant_id, :decision_row_id, :terminal_event_id, :gateway_id, :session_id,
        :decision_id, :skill_call_id, :skill_name, :status, :failure_category,
        :reason, :retryable, :effect_status, clock_timestamp()
    )
    RETURNING id
    """
)

_UPDATE_TERMINAL = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET terminal_event_id = :terminal_event_id,
        status = :status,
        failure_category = :failure_category,
        reason = :reason,
        retryable = :retryable,
        effect_status = :effect_status,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id AND status IN ('pending', 'started')
    RETURNING id
    """
)

_MARK_TERMINAL_CONFLICT = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET status = 'manual',
        failure_category = 'internal_failed',
        reason = 'terminal_conflict',
        retryable = false,
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    """
)

_MARK_CALL_CONFLICT = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET status = 'manual',
        failure_category = 'protocol_failed',
        reason = 'skill_call_identity_conflict',
        retryable = false,
        completed_at = COALESCE(completed_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = :row_id
    """
)

_MARK_EFFECT_APPLIED = sa.text(
    """
    UPDATE llm_gateway_skill_calls
    SET effect_status = 'applied',
        effect_applied_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :row_id AND effect_status = 'pending'
    RETURNING id
    """
)


class TerminalRepository:
    def __init__(
        self,
        session_factory: _SessionFactory | Callable[[], AsyncSession] = async_session_factory,
        *,
        effect_service: TerminalEffectService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._effect_service = effect_service or TerminalEffectService()

    async def record_skill_started(self, claimed: ClaimedGatewayEvent) -> MutationResult:
        event = claimed.event
        if not isinstance(event, SkillStartedEvent):
            raise ValueError("skill_started event is required")
        async with self._session_factory() as session, session.begin():
            decision = await self._lock_decision(session, claimed.gateway_id, event.payload.decision_id)
            if decision is None:
                return MutationResult(MutationDisposition.MISSING, "missing_decision")
            skill_name = self._skill_name(decision)
            if skill_name is None:
                return MutationResult(MutationDisposition.CONFLICT, "decision_action_mismatch")
            call = await self._lock_call(session, claimed.gateway_id, event.payload.skill_call_id)
            if call is None:
                await session.execute(
                    _INSERT_STARTED,
                    {
                        "tenant_id": claimed.tenant_id,
                        "decision_row_id": decision["id"],
                        "gateway_id": claimed.gateway_id,
                        "session_id": claimed.session_id,
                        "decision_id": event.payload.decision_id,
                        "skill_call_id": event.payload.skill_call_id,
                        "skill_name": skill_name,
                    },
                )
                return MutationResult(MutationDisposition.APPLIED)
            if not self._call_matches_decision(call, decision):
                await session.execute(_MARK_CALL_CONFLICT, {"row_id": call["id"]})
                return MutationResult(MutationDisposition.CONFLICT, "skill_call_identity_conflict")
            if str(call["status"]) == "pending":
                await session.execute(_MARK_STARTED, {"row_id": call["id"]})
                return MutationResult(MutationDisposition.APPLIED)
            if str(call["status"]) in {
                "started",
                "succeeded",
                "failed",
                "cancelled",
                "timeout",
            }:
                return MutationResult(MutationDisposition.IDEMPOTENT)
            return MutationResult(MutationDisposition.CONFLICT, "skill_call_state_conflict")

    async def record_skill_finished(self, claimed: ClaimedGatewayEvent) -> MutationResult:
        event = claimed.event
        if not isinstance(event, SkillFinishedEvent):
            raise ValueError("skill_finished event is required")
        incoming = normalize_skill_terminal(event.payload.terminal).with_terminal_event_id(event.event_id)
        async with self._session_factory() as session, session.begin():
            decision = await self._lock_decision(session, claimed.gateway_id, event.payload.decision_id)
            if decision is None:
                return MutationResult(MutationDisposition.MISSING, "missing_decision")
            skill_name = self._skill_name(decision)
            if skill_name is None:
                return MutationResult(MutationDisposition.CONFLICT, "decision_action_mismatch")
            call = await self._lock_call(session, claimed.gateway_id, event.payload.skill_call_id)
            if call is not None and not self._call_matches_decision(call, decision):
                await session.execute(_MARK_CALL_CONFLICT, {"row_id": call["id"]})
                return MutationResult(MutationDisposition.CONFLICT, "skill_call_identity_conflict")

            action_tracking_id = self._optional_uuid(decision["action_tracking_id"])
            effect_status = "pending" if action_tracking_id is not None else "not_applicable"
            if call is None:
                call_id = (
                    await session.execute(
                        _INSERT_TERMINAL,
                        {
                            "tenant_id": claimed.tenant_id,
                            "decision_row_id": decision["id"],
                            "terminal_event_id": claimed.row_id,
                            "gateway_id": claimed.gateway_id,
                            "session_id": claimed.session_id,
                            "decision_id": event.payload.decision_id,
                            "skill_call_id": event.payload.skill_call_id,
                            "skill_name": skill_name,
                            "status": incoming.status,
                            "failure_category": incoming.failure_category,
                            "reason": incoming.reason,
                            "retryable": incoming.retryable,
                            "effect_status": effect_status,
                        },
                    )
                ).scalar_one()
                await self._apply_effect(session, call_id, action_tracking_id, incoming.status)
                return MutationResult(MutationDisposition.APPLIED)

            existing = self._terminal_from_row(call)
            transition = resolve_terminal_transition(existing, incoming)
            if transition.disposition is MutationDisposition.IDEMPOTENT:
                return MutationResult(MutationDisposition.IDEMPOTENT)
            if transition.disposition is MutationDisposition.CONFLICT:
                await session.execute(_MARK_TERMINAL_CONFLICT, {"row_id": call["id"]})
                return MutationResult(MutationDisposition.CONFLICT, "terminal_conflict")

            updated = await session.execute(
                _UPDATE_TERMINAL,
                {
                    "row_id": call["id"],
                    "terminal_event_id": claimed.row_id,
                    "status": incoming.status,
                    "failure_category": incoming.failure_category,
                    "reason": incoming.reason,
                    "retryable": incoming.retryable,
                    "effect_status": effect_status,
                },
            )
            if updated.scalar_one_or_none() is None:
                return MutationResult(MutationDisposition.CONFLICT, "terminal_state_changed")
            await self._apply_effect(session, call["id"], action_tracking_id, incoming.status)
            return MutationResult(MutationDisposition.APPLIED)

    async def _apply_effect(
        self,
        session: AsyncSession,
        call_id: object,
        action_tracking_id: UUID | None,
        terminal_status: str,
    ) -> None:
        if action_tracking_id is None:
            return
        await self._effect_service.apply(
            session,
            action_tracking_id=action_tracking_id,
            terminal_status=terminal_status,
        )
        applied = await session.execute(_MARK_EFFECT_APPLIED, {"row_id": call_id})
        if applied.scalar_one_or_none() is None:
            raise RuntimeError("skill-call terminal effect was not committed")

    @staticmethod
    async def _lock_decision(
        session: AsyncSession,
        gateway_id: str,
        decision_id: str,
    ) -> RowMapping | None:
        result = await session.execute(
            _LOCK_DECISION,
            {"gateway_id": gateway_id, "decision_id": decision_id},
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _lock_call(
        session: AsyncSession,
        gateway_id: str,
        skill_call_id: str,
    ) -> RowMapping | None:
        result = await session.execute(
            _LOCK_SKILL_CALL,
            {"gateway_id": gateway_id, "skill_call_id": skill_call_id},
        )
        return result.mappings().one_or_none()

    @staticmethod
    def _skill_name(decision: RowMapping) -> str | None:
        action = str(decision["action"])
        if action == "stop_hosting":
            return "stop_hosting"
        if action != "call_skill":
            return None
        body = decision["request_body_json"]
        if not isinstance(body, Mapping):
            return None
        skill_name = body.get("skillName")
        return skill_name if isinstance(skill_name, str) and skill_name else None

    @staticmethod
    def _call_matches_decision(call: RowMapping, decision: RowMapping) -> bool:
        return str(call["decision_row_id"]) == str(decision["id"]) and str(call["decision_id"]) == str(
            decision["decision_id"]
        )

    @staticmethod
    def _terminal_from_row(row: RowMapping) -> TerminalRecord:
        terminal_event_id = row["terminal_event_id"]
        return TerminalRecord(
            status=str(row["status"]),
            failure_category=None if row["failure_category"] is None else str(row["failure_category"]),
            reason=None if row["reason"] is None else str(row["reason"]),
            retryable=None if row["retryable"] is None else bool(row["retryable"]),
            terminal_event_id=None if terminal_event_id is None else str(terminal_event_id),
        )

    @staticmethod
    def _optional_uuid(value: object) -> UUID | None:
        if value is None:
            return None
        return value if isinstance(value, UUID) else UUID(str(value))
