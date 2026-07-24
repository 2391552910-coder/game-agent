from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from src.core.agents.gateway_v2_models import (
    GatewayV2AgentAction,
    GatewayV2AgentContext,
    GatewayV2CallSkillAction,
    GatewayV2NoOpAction,
    GatewayV2StopHostingAction,
    GatewayV2WaitAction,
    parse_gateway_v2_agent_action,
)
from src.core.integration.llm_gateway_v2.canonical import canonical_json_bytes
from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2Decision,
    GatewayV2Event,
    ObservationUpdatedEvent,
    SessionStartedEvent,
    SkillFinishedEvent,
    parse_gateway_v2_decision,
)
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent, EventProcessResult
from src.core.integration.llm_gateway_v2.outbox_repository import (
    DecisionPlanConflictError,
    DecisionPlanFencedError,
    DecisionPlanUnavailableError,
    PlannedDecision,
)


class GatewayV2DecisionSelectionError(Exception):
    def __init__(self) -> None:
        super().__init__("gateway v2 lease has no permitted decision")


class GatewayV2AgentExecutionError(Exception):
    retryable = True
    stage = "agent"

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("gateway v2 agent execution failed")


class GatewayV2AgentRunner(Protocol):
    async def ainvoke(self, state: dict[str, Any]) -> Mapping[str, Any]: ...


def build_gateway_v2_agent_context(event: GatewayV2Event) -> GatewayV2AgentContext:
    terminal_result: Mapping[str, Any] | None = None
    if isinstance(event, (SessionStartedEvent, ObservationUpdatedEvent)):
        lease = event.payload.lease
    elif isinstance(event, SkillFinishedEvent) and event.payload.lease is not None:
        lease = event.payload.lease
        terminal_result = event.payload.terminal.model_dump(mode="json", by_alias=True)
    else:
        raise ValueError("event does not carry a decision lease")

    return GatewayV2AgentContext(
        event_id=event.event_id,
        session_id=event.session_id,
        control_generation=event.control_generation,
        event_sequence=event.event_sequence,
        decision_lease_id=lease.decision_lease_id,
        state_version=lease.state_version,
        lease_kind=lease.lease_kind,
        allowed_decision_actions=lease.allowed_decision_actions,
        session_snapshot=lease.session,
        available_skills=lease.available_skills,
        skill_argument_hints=lease.skill_argument_hints,
        terminal_result=terminal_result,
    )


def _argument_leaf_paths(value: Mapping[str, Any], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for key, item in value.items():
        if not key or "." in key:
            raise ValueError("argument object keys must be non-empty path segments")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            nested = _argument_leaf_paths(item, path)
            if nested:
                paths.update(nested)
            else:
                paths.add(path)
        else:
            paths.add(path)
    return paths


def _contains_path(paths: set[str], required: str) -> bool:
    return required in paths or any(path.startswith(f"{required}.") for path in paths)


def _skill_is_permitted(
    context: GatewayV2AgentContext,
    candidate: GatewayV2CallSkillAction,
) -> bool:
    if "call_skill" not in context.allowed_decision_actions:
        return False
    if candidate.skill_name == "ground":
        return False
    if context.lease_kind == "movement_control" and candidate.skill_name not in {"jump", "stop_move"}:
        return False
    tracking_metadata = candidate.tracking_metadata()
    if tracking_metadata is not None:
        account_id = context.session_snapshot.get("accountId")
        if tracking_metadata.user_id != account_id or tracking_metadata.action_type != candidate.skill_name:
            return False

    available = {(skill.skill_name, skill.schema_version) for skill in context.available_skills}
    identity = (candidate.skill_name, candidate.schema_version)
    if identity not in available:
        return False

    hints = {(hint.skill_name, hint.schema_version): hint for hint in context.skill_argument_hints}
    hint = hints.get(identity)
    try:
        argument_paths = _argument_leaf_paths(candidate.arguments)
    except ValueError:
        return False
    if hint is None:
        return not argument_paths

    if not argument_paths.issubset(set(hint.allowed_args)):
        return False
    return all(_contains_path(argument_paths, required) for required in hint.missing_args)


def select_gateway_v2_action(
    context: GatewayV2AgentContext,
    candidates: Sequence[GatewayV2AgentAction],
) -> GatewayV2AgentAction:
    allowed_actions = set(context.allowed_decision_actions)
    for candidate in candidates:
        if isinstance(candidate, GatewayV2CallSkillAction):
            if _skill_is_permitted(context, candidate):
                return candidate
            continue
        if candidate.action in allowed_actions:
            return candidate

    if "wait" in allowed_actions:
        return GatewayV2WaitAction(reason="No authorized skill candidate", waitMs=1_000)
    if "no_op" in allowed_actions:
        return GatewayV2NoOpAction(reason="No authorized skill candidate")
    if "stop_hosting" in allowed_actions:
        return GatewayV2StopHostingAction(reason="No authorized skill candidate")
    raise GatewayV2DecisionSelectionError


class GatewayV2DecisionService:
    def __init__(self, *, runner: GatewayV2AgentRunner | None = None) -> None:
        if runner is None:
            from src.core.agents.gateway_v2 import build_gateway_v2_decision_graph

            runner = cast(GatewayV2AgentRunner, build_gateway_v2_decision_graph().compile())
        self._runner: GatewayV2AgentRunner = runner

    async def decide(
        self,
        context: GatewayV2AgentContext,
        *,
        user_id: str,
        tenant_id: str,
    ) -> GatewayV2AgentAction:
        initial_state = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "snapshot": context.prompt_payload()["session"],
            "gateway_context": context.model_dump(mode="json"),
            "rag_context": "",
            "enriched_context": "",
            "behavior_report": "",
            "reasoned_actions": [],
            "errors": [],
            "tracking_summary": "",
            "anomalies": [],
            "abandoned_tracking_ids": [],
            "intent_result": {},
            "goal_evaluation_result": {},
            "player_memory": {},
        }
        try:
            result = await self._runner.ainvoke(initial_state)
        except Exception:
            raise GatewayV2AgentExecutionError("execution_failed") from None

        if result.get("errors"):
            raise GatewayV2AgentExecutionError("node_error")
        selected = result.get("selected_action")
        if selected is None:
            raise GatewayV2AgentExecutionError("empty_output")
        try:
            return parse_gateway_v2_agent_action(selected)
        except Exception:
            raise GatewayV2AgentExecutionError("invalid_output") from None


@dataclass(frozen=True)
class FrozenGatewayV2Decision:
    decision: GatewayV2Decision
    body_json: dict[str, Any]
    body_bytes: bytes
    body_hash: str

    @property
    def canonical_text(self) -> str:
        return self.body_bytes.decode("utf-8")


def freeze_gateway_v2_decision(
    decision_id: str,
    context: GatewayV2AgentContext,
    action: GatewayV2AgentAction,
) -> FrozenGatewayV2Decision:
    payload: dict[str, Any] = {
        "contractVersion": "llm-gateway-http-v2",
        "decisionId": decision_id,
        "decisionLeaseId": context.decision_lease_id,
        "stateVersion": context.state_version,
        "controlGeneration": context.control_generation,
        "ttlMs": action.ttl_ms,
        "action": action.action,
    }
    if isinstance(action, GatewayV2CallSkillAction):
        payload.update(
            {
                "skillName": action.skill_name,
                "schemaVersion": action.schema_version,
                "arguments": action.model_dump(mode="json", by_alias=True)["arguments"],
            }
        )
    elif isinstance(action, GatewayV2WaitAction):
        payload["waitMs"] = action.wait_ms

    decision = parse_gateway_v2_decision(payload)
    body_json = decision.model_dump(mode="json", by_alias=True)
    body_bytes = canonical_json_bytes(body_json)
    return FrozenGatewayV2Decision(
        decision=decision,
        body_json=body_json,
        body_bytes=body_bytes,
        body_hash=hashlib.sha256(body_bytes).hexdigest(),
    )


class _DecisionPlanRepository(Protocol):
    async def find_by_source_event(self, event: ClaimedGatewayEvent) -> PlannedDecision | None: ...

    async def plan_decision(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
        action: GatewayV2AgentAction,
    ) -> PlannedDecision: ...


@dataclass(frozen=True)
class GatewayV2DecisionPlanner:
    decision_service: GatewayV2DecisionService
    repository: _DecisionPlanRepository

    async def __call__(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> EventProcessResult:
        try:
            existing = await self.repository.find_by_source_event(event)
            if existing is not None:
                return EventProcessResult("succeeded")

            account_id = context.session_snapshot.get("accountId")
            if not isinstance(account_id, str) or not account_id.strip():
                raise GatewayV2AgentExecutionError("missing_account_id")
            action = await self.decision_service.decide(
                context,
                user_id=account_id,
                tenant_id=str(event.tenant_id),
            )
            await self.repository.plan_decision(event, context, action)
            return EventProcessResult("succeeded")
        except DecisionPlanFencedError:
            return EventProcessResult("manual", error_stage="fence", error_category="claim_lost")
        except DecisionPlanConflictError as error:
            return EventProcessResult("manual", error_stage="plan", error_category=error.category)
        except DecisionPlanUnavailableError:
            return EventProcessResult(
                "retryable_failed",
                error_stage="database",
                error_category="plan_unavailable",
            )
