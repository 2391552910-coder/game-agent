from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanRepository,
    ActivityPlanUnavailableError,
)
from src.core.integration.llm_gateway_v2.auth import InboundGatewayIdentity
from src.core.integration.llm_gateway_v2.contracts import (
    ChatReceivedEvent,
    ChatSendResultEvent,
    DecisionRejectedEvent,
    GatewayV2BatchAck,
    GatewayV2BatchEnvelope,
    GatewayV2Event,
    NearbyFriendChatRequestedEvent,
    ObservationUpdatedEvent,
    SessionStartedEvent,
    SessionStoppedEvent,
    SkillFinishedEvent,
    SkillStartedEvent,
)
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2AgentExecutionError,
    build_gateway_v2_agent_context,
)
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent, EventProcessResult
from src.core.integration.llm_gateway_v2.hosted_chat import HostedChatPermanentError, HostedChatRetryableError
from src.core.integration.llm_gateway_v2.inbox_repository import (
    BatchAcceptance,
    EventAdmissionConflict,
    EventAdmissionUnavailable,
    InboxRepository,
)
from src.core.integration.llm_gateway_v2.terminal_repository import MutationDisposition, MutationResult
from src.core.integration.llm_gateway_v2.worker_hooks import NO_OP_WORKER_HOOKS, WorkerHooks

logger = logging.getLogger(__name__)


class EventBatchInvalid(Exception):  # noqa: N818 - protocol-domain name
    def __init__(self) -> None:
        super().__init__()


class EventContentConflict(Exception):  # noqa: N818 - protocol-domain name
    def __init__(self) -> None:
        super().__init__()


class EventServiceUnavailable(Exception):  # noqa: N818 - protocol-domain name
    def __init__(self) -> None:
        super().__init__()


class _InboxRepository(Protocol):
    async def accept_event_batch(
        self,
        identity: InboundGatewayIdentity,
        trace_id: str,
        events: Sequence[GatewayV2Event],
    ) -> BatchAcceptance: ...


class _LeaseContextRepository(Protocol):
    async def persist_lease_context(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> bool: ...


class _TerminalRepository(Protocol):
    async def record_skill_started(self, event: ClaimedGatewayEvent) -> MutationResult: ...

    async def record_skill_finished(self, event: ClaimedGatewayEvent) -> MutationResult: ...


class _OutboxRepository(Protocol):
    async def merge_decision_rejected(self, event: ClaimedGatewayEvent) -> MutationResult: ...

    async def close_generation(self, event: ClaimedGatewayEvent) -> MutationResult: ...


class _HostedChatService(Protocol):
    async def handle_chat_received(self, gateway_id: str, event: ChatReceivedEvent) -> None: ...

    async def handle_nearby_friend_request(self, gateway_id: str, event: NearbyFriendChatRequestedEvent) -> None: ...

    async def handle_send_result(self, gateway_id: str, event: ChatSendResultEvent) -> None: ...


class _ActivityPlanRepository(Protocol):
    async def record_observation(self, event: ClaimedGatewayEvent) -> bool: ...

    async def record_skill_started(self, event: ClaimedGatewayEvent) -> bool: ...

    async def record_skill_finished(self, event: ClaimedGatewayEvent) -> bool: ...

    async def record_decision_rejected(self, event: ClaimedGatewayEvent) -> bool: ...

    async def record_chat_opportunity(self, event: ClaimedGatewayEvent) -> bool: ...

    async def complete_passive_step(self, event: ClaimedGatewayEvent) -> bool: ...

    async def close(self, event: ClaimedGatewayEvent) -> bool: ...


DecisionPlanner = Callable[
    [ClaimedGatewayEvent, GatewayV2AgentContext],
    Awaitable[EventProcessResult],
]


@dataclass(frozen=True)
class GatewayV2EventDispatcher:
    context_repository: _LeaseContextRepository
    terminal_repository: _TerminalRepository
    outbox_repository: _OutboxRepository
    decision_planner: DecisionPlanner
    hosted_chat_service: _HostedChatService | None = None
    activity_repository: _ActivityPlanRepository | ActivityPlanRepository | None = None

    async def __call__(self, claimed: ClaimedGatewayEvent) -> EventProcessResult:
        try:
            event = claimed.event
            if isinstance(event, ChatReceivedEvent):
                if self.hosted_chat_service is None:
                    return EventProcessResult("manual", error_stage="chat", error_category="chat_not_configured")
                await self.hosted_chat_service.handle_chat_received(claimed.gateway_id, event)
                return EventProcessResult("succeeded")
            if isinstance(event, NearbyFriendChatRequestedEvent):
                if self.hosted_chat_service is None:
                    return EventProcessResult("manual", error_stage="chat", error_category="chat_not_configured")
                await self.hosted_chat_service.handle_nearby_friend_request(claimed.gateway_id, event)
                if self.activity_repository is not None and not claimed.historical_recovery:
                    await self.activity_repository.record_chat_opportunity(claimed)
                return EventProcessResult("succeeded")
            if isinstance(event, ChatSendResultEvent):
                if self.hosted_chat_service is None:
                    return EventProcessResult("manual", error_stage="chat", error_category="chat_not_configured")
                await self.hosted_chat_service.handle_send_result(claimed.gateway_id, event)
                return EventProcessResult("succeeded")
            if isinstance(event, SessionStartedEvent):
                return await self._process_lease_event(claimed)
            if isinstance(event, ObservationUpdatedEvent):
                if self.activity_repository is not None and not claimed.historical_recovery:
                    await self.activity_repository.record_observation(claimed)
                return await self._process_lease_event(claimed)
            if isinstance(event, SkillStartedEvent):
                mutation = await self.terminal_repository.record_skill_started(claimed)
                outcome = self._mutation_result(mutation, stage="terminal")
                if (
                    outcome.outcome == "succeeded"
                    and self.activity_repository is not None
                    and not claimed.historical_recovery
                ):
                    await self.activity_repository.record_skill_started(claimed)
                return outcome
            if isinstance(event, SkillFinishedEvent):
                mutation = await self.terminal_repository.record_skill_finished(claimed)
                outcome = self._mutation_result(mutation, stage="terminal")
                if (
                    outcome.outcome == "succeeded"
                    and self.activity_repository is not None
                    and not claimed.historical_recovery
                ):
                    await self.activity_repository.record_skill_finished(claimed)
                    await self.activity_repository.complete_passive_step(claimed)
                if (
                    outcome.outcome != "succeeded"
                    or event.payload.lease is None
                    or claimed.historical_recovery
                ):
                    return outcome
                return await self._process_lease_event(claimed)
            if isinstance(event, DecisionRejectedEvent):
                mutation = await self.outbox_repository.merge_decision_rejected(claimed)
                outcome = self._mutation_result(mutation, stage="decision")
                if (
                    outcome.outcome == "succeeded"
                    and self.activity_repository is not None
                    and not claimed.historical_recovery
                ):
                    await self.activity_repository.record_decision_rejected(claimed)
                return outcome
            if isinstance(event, SessionStoppedEvent):
                if self.activity_repository is not None:
                    await self.activity_repository.close(claimed)
                mutation = await self.outbox_repository.close_generation(claimed)
                return self._mutation_result(mutation, stage="session")
            return EventProcessResult("manual", error_stage="contract", error_category="unsupported_event")
        except GatewayV2AgentExecutionError as error:
            logger.error(
                "LLM Gateway v2 Agent event processing failed",
                extra=safe_exception_fields(
                    stage=error.stage,
                    category=error.category,
                    error=error,
                    trace_id=claimed.trace_id,
                    event_id=claimed.event_id,
                ),
            )
            return EventProcessResult(
                "retryable_failed",
                error_stage=error.stage,
                error_category=error.category,
            )
        except HostedChatRetryableError as error:
            return EventProcessResult("retryable_failed", error_stage="chat", error_category=error.category)
        except HostedChatPermanentError as error:
            return EventProcessResult("manual", error_stage="chat", error_category=error.category)
        except ActivityPlanUnavailableError:
            return EventProcessResult(
                "retryable_failed",
                error_stage="database",
                error_category="activity_plan_unavailable",
            )
        except Exception as error:
            logger.error(
                "LLM Gateway v2 event operation failed",
                extra=safe_exception_fields(
                    stage="database",
                    category="operation_failed",
                    error=error,
                    trace_id=claimed.trace_id,
                    event_id=claimed.event_id,
                ),
                exc_info=True,
            )
            return EventProcessResult(
                "retryable_failed",
                error_stage="database",
                error_category="operation_failed",
            )

    async def _process_lease_event(self, claimed: ClaimedGatewayEvent) -> EventProcessResult:
        context = build_gateway_v2_agent_context(claimed.event)
        persisted = await self.context_repository.persist_lease_context(claimed, context)
        if not persisted:
            return EventProcessResult("manual", error_stage="fence", error_category="claim_lost")
        return await self.decision_planner(claimed, context)

    @staticmethod
    def _mutation_result(mutation: MutationResult, *, stage: str) -> EventProcessResult:
        if mutation.disposition in {MutationDisposition.APPLIED, MutationDisposition.IDEMPOTENT}:
            return EventProcessResult("succeeded")
        if mutation.disposition is MutationDisposition.MISSING:
            return EventProcessResult(
                "retryable_failed",
                error_stage=stage,
                error_category=mutation.error_category or "missing_dependency",
            )
        if mutation.disposition is MutationDisposition.FENCED:
            return EventProcessResult(
                "manual",
                error_stage="fence",
                error_category=mutation.error_category or "claim_lost",
            )
        return EventProcessResult(
            "manual",
            error_stage=stage,
            error_category=mutation.error_category or "state_conflict",
        )


@dataclass(frozen=True)
class EventService:
    repository: _InboxRepository
    max_batch_size: int | None = None
    hooks: WorkerHooks = NO_OP_WORKER_HOOKS

    async def accept_event_batch(
        self,
        identity: InboundGatewayIdentity,
        envelope: GatewayV2BatchEnvelope,
    ) -> GatewayV2BatchAck:
        if not isinstance(identity.tenant_id, UUID):
            raise EventBatchInvalid
        if self.max_batch_size is not None and (
            type(self.max_batch_size) is not int
            or self.max_batch_size < 1
            or len(envelope.events) > self.max_batch_size
        ):
            raise EventBatchInvalid

        started = time.monotonic()
        try:
            acceptance = await self.repository.accept_event_batch(identity, envelope.trace_id, envelope.events)
        except EventAdmissionConflict:
            logger.warning(
                "LLM Gateway v2 event admission conflict",
                extra={
                    "trace_id": envelope.trace_id,
                    "gateway_id": identity.gateway_id,
                    "event_count": len(envelope.events),
                    "elapsed_ms": (time.monotonic() - started) * 1_000,
                },
            )
            raise EventContentConflict from None
        except EventAdmissionUnavailable:
            logger.error(
                "LLM Gateway v2 event admission unavailable",
                extra={
                    "trace_id": envelope.trace_id,
                    "gateway_id": identity.gateway_id,
                    "event_count": len(envelope.events),
                    "elapsed_ms": (time.monotonic() - started) * 1_000,
                },
            )
            raise EventServiceUnavailable from None

        await self.hooks.after_event_commit(
            acceptance.received_event_ids + acceptance.duplicate_event_ids
        )

        input_order = tuple(dict.fromkeys(event.event_id for event in envelope.events))
        received = set(acceptance.received_event_ids)
        duplicate = set(acceptance.duplicate_event_ids) - received
        ack = GatewayV2BatchAck.model_validate(
            {
                "accepted": True,
                "traceId": envelope.trace_id,
                "receivedEventIds": [event_id for event_id in input_order if event_id in received],
                "duplicateEventIds": [event_id for event_id in input_order if event_id in duplicate],
            }
        )
        logger.info(
            "LLM Gateway v2 event admission committed",
            extra={
                "trace_id": envelope.trace_id,
                "gateway_id": identity.gateway_id,
                "event_count": len(envelope.events),
                "received_count": len(acceptance.received_event_ids),
                "duplicate_count": len(acceptance.duplicate_event_ids),
                "elapsed_ms": (time.monotonic() - started) * 1_000,
            },
        )
        return ack

    async def accept_batch(
        self,
        identity: InboundGatewayIdentity,
        envelope: GatewayV2BatchEnvelope,
        *,
        max_batch_size: int,
    ) -> GatewayV2BatchAck:
        service = EventService(self.repository, max_batch_size=max_batch_size)
        return await service.accept_event_batch(identity, envelope)


GatewayV2EventService = EventService


def build_gateway_v2_event_service(max_batch_size: int | None = None) -> EventService:
    return EventService(InboxRepository(), max_batch_size=max_batch_size)
