from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
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
from src.core.integration.llm_gateway_v2.activity_capacity import ActivityCapacitySnapshot
from src.core.integration.llm_gateway_v2.activity_capacity_repository import (
    ActivityCapacityRepository,
    ActivityCapacityUnavailableError,
)
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanBinding,
    ActivityPlanContext,
)
from src.core.integration.llm_gateway_v2.activity_planner import ActivityPlanCoordinator
from src.core.integration.llm_gateway_v2.canonical import canonical_json_bytes
from src.core.integration.llm_gateway_v2.capacity import AgentCapacityExceededError, AgentCapacityLimiter
from src.core.integration.llm_gateway_v2.competitive_activity import (
    build_dance_arguments,
    build_darts_arguments,
    build_shooting_arguments,
    competitive_activity_seed,
)
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
    ActivityCapacityFullError,
    DecisionPlanConflictError,
    DecisionPlanFencedError,
    DecisionPlanUnavailableError,
    PlannedDecision,
)
from src.core.integration.llm_gateway_v2.paper_plane import (
    build_paper_plane_arguments,
    paper_plane_arguments_seed,
)
from src.core.integration.llm_gateway_v2.runtime_metrics import GatewayV2RuntimeMetrics
from src.core.integration.llm_gateway_v2.scene_catalog import (
    SceneCatalog,
    scene_id_from_snapshot,
)
from src.core.integration.llm_gateway_v2.token_usage import (
    GatewayV2TokenUsageTracker,
    gateway_v2_token_usage_tracker,
)

logger = logging.getLogger(__name__)

_SPECIALIZED_ARGUMENT_SKILLS = frozenset(
    {
        "paper_plane_auto_schedule",
        "darts_auto_schedule",
        "shooting_auto_schedule",
        "dance_auto_schedule",
    }
)

_REQUIRED_ACTIVITY_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "wish_board_auto_schedule": ("boardName", "wish"),
    "coffee_auto_schedule": ("coffeeName",),
    "seat_sit": ("sceneId", "chairId"),
    "seat_get_out": ("sceneId", "chairId"),
}

_LOCAL_FALLBACK_SKILL_ORDER: tuple[str, ...] = (
    "dance_auto_schedule",
    "hot_air_balloon_auto_schedule",
    "coffee_auto_schedule",
    "darts_auto_schedule",
    "shooting_auto_schedule",
    "paper_plane_auto_schedule",
    "draw_lots_auto_schedule",
    "wish_board_auto_schedule",
    "helicopter_auto_schedule",
    "elevator_auto_schedule",
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
        decision_context = event.payload.decision_context
        terminal_result = decision_context.last_skill_result
    elif isinstance(event, SkillFinishedEvent) and event.payload.lease is not None:
        lease = event.payload.lease
        assert event.payload.decision_context is not None
        decision_context = event.payload.decision_context
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
        allowed_decision_actions=lease.allowed_actions,
        parent_skill_name=lease.parent_skill_name,
        allowed_skill_name=lease.allowed_skill_name,
        allowed_skill_names=lease.allowed_skill_names,
        session_snapshot=decision_context.session,
        available_skills=decision_context.available_skills,
        skill_argument_hints=decision_context.skill_argument_hints,
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


def _required_activity_arguments_are_valid(
    skill_name: str,
    arguments: Mapping[str, Any],
) -> bool:
    required = _REQUIRED_ACTIVITY_ARGUMENTS.get(skill_name)
    if required is None:
        return True
    if any(name not in arguments for name in required):
        return False
    if skill_name == "wish_board_auto_schedule":
        return all(isinstance(arguments[name], str) and bool(arguments[name].strip()) for name in required)
    if skill_name == "coffee_auto_schedule":
        value = arguments["coffeeName"]
        return isinstance(value, str) and bool(value.strip())
    return all(type(arguments[name]) is int and arguments[name] > 0 for name in required)


def _paper_plane_arguments_for_context(context: GatewayV2AgentContext) -> dict[str, Any]:
    snapshot = context.session_snapshot
    account_id = snapshot.get("AccountId", snapshot.get("accountId"))
    return build_paper_plane_arguments(
        seed=paper_plane_arguments_seed(
            session_id=context.session_id,
            event_id=context.event_id,
            account_id=account_id if isinstance(account_id, str) else None,
            control_generation=context.control_generation,
            event_sequence=context.event_sequence,
            decision_lease_id=context.decision_lease_id,
            state_version=context.state_version,
        )
    )


def _competitive_activity_seed_for_context(
    context: GatewayV2AgentContext,
    skill_name: str,
) -> str:
    snapshot = context.session_snapshot
    account_id = snapshot.get("AccountId", snapshot.get("accountId"))
    return competitive_activity_seed(
        skill_name=skill_name,
        session_id=context.session_id,
        event_id=context.event_id,
        account_id=account_id if isinstance(account_id, str) else None,
        control_generation=context.control_generation,
        event_sequence=context.event_sequence,
        decision_lease_id=context.decision_lease_id,
        state_version=context.state_version,
    )


def _skill_argument_hint(
    context: GatewayV2AgentContext,
    skill_name: str,
    schema_version: str,
):
    return next(
        (
            hint
            for hint in context.skill_argument_hints
            if hint.skill_name == skill_name and hint.schema_version == schema_version
        ),
        None,
    )


def _specialized_arguments_for_context(
    context: GatewayV2AgentContext,
    skill_name: str,
    schema_version: str,
) -> dict[str, Any] | None:
    if schema_version != "v1":
        return None
    if skill_name == "paper_plane_auto_schedule":
        return _paper_plane_arguments_for_context(context)
    seed = _competitive_activity_seed_for_context(context, skill_name)
    if skill_name == "darts_auto_schedule":
        return build_darts_arguments(seed=seed)
    if skill_name == "shooting_auto_schedule":
        return build_shooting_arguments(seed=seed)
    if skill_name == "dance_auto_schedule":
        return build_dance_arguments(seed=seed)
    return None


def _safe_unavailable_skill_action(context: GatewayV2AgentContext) -> GatewayV2AgentAction:
    reason = "Gateway did not provide complete constraints for the selected skill"
    if "wait" in context.allowed_decision_actions:
        return GatewayV2WaitAction(reason=reason, waitMs=1_000)
    if "no_op" in context.allowed_decision_actions:
        return GatewayV2NoOpAction(reason=reason)
    if "stop_hosting" in context.allowed_decision_actions:
        return GatewayV2StopHostingAction(reason=reason)
    raise GatewayV2DecisionSelectionError


def _normalize_gateway_v2_action(
    context: GatewayV2AgentContext,
    action: GatewayV2AgentAction,
) -> GatewayV2AgentAction:
    if not isinstance(action, GatewayV2CallSkillAction):
        return action
    if action.skill_name not in _SPECIALIZED_ARGUMENT_SKILLS:
        return action
    if action.schema_version != "v1":
        return action
    arguments = _specialized_arguments_for_context(
        context,
        action.skill_name,
        action.schema_version,
    )
    if arguments is None:
        return action
    normalized = action.model_dump(mode="json", by_alias=True)
    normalized["arguments"] = arguments
    return GatewayV2CallSkillAction.model_validate(normalized)


def _lease_pairing_is_permitted(
    context: GatewayV2AgentContext,
    candidate: GatewayV2CallSkillAction,
) -> bool:
    paired_exits = {
        "hot_air_balloon_auto_schedule": "hot_air_balloon_exit",
        "helicopter_auto_schedule": "helicopter_exit",
    }
    if context.lease_kind == "observation":
        return candidate.skill_name not in set(paired_exits.values())

    if context.lease_kind == "movement_control":
        if candidate.skill_name not in {"jump", "stop_move"}:
            return False
        return candidate.skill_name != "stop_move" or context.parent_skill_name == "move_to"

    if context.lease_kind == "vehicle_cancel_window":
        return (
            context.parent_skill_name is not None
            and paired_exits.get(context.parent_skill_name) == candidate.skill_name
        )

    if context.lease_kind == "vehicle_recovery":
        return candidate.skill_name == "observe_state" or (
            context.parent_skill_name is not None
            and paired_exits.get(context.parent_skill_name) == candidate.skill_name
        )

    if context.lease_kind == "conversation":
        return candidate.skill_name == "nearby_chat_send"

    return False


def _skill_is_permitted(
    context: GatewayV2AgentContext,
    candidate: GatewayV2CallSkillAction,
) -> bool:
    if "call_skill" not in context.allowed_decision_actions:
        return False
    if candidate.skill_name == "ground":
        return False
    if context.lease_kind == "observation":
        allowed_skill_names = {skill.skill_name for skill in context.available_skills}
    else:
        allowed_skill_names = set(context.allowed_skill_names)
        if context.allowed_skill_name is not None:
            allowed_skill_names.add(context.allowed_skill_name)
    if candidate.skill_name not in allowed_skill_names:
        return False
    if not _lease_pairing_is_permitted(context, candidate):
        return False
    tracking_metadata = candidate.tracking_metadata()
    if tracking_metadata is not None:
        account_id = context.session_snapshot.get("AccountId", context.session_snapshot.get("accountId"))
        if tracking_metadata.user_id != account_id or tracking_metadata.action_type != candidate.skill_name:
            return False

    available = {(skill.skill_name, skill.schema_version) for skill in context.available_skills}
    identity = (candidate.skill_name, candidate.schema_version)
    if identity not in available:
        return False

    hints = {(hint.skill_name, hint.schema_version): hint for hint in context.skill_argument_hints}
    hint = hints.get(identity)
    if candidate.skill_name in _SPECIALIZED_ARGUMENT_SKILLS:
        specialized_arguments = _specialized_arguments_for_context(
            context,
            candidate.skill_name,
            candidate.schema_version,
        )
        serialized_arguments = candidate.model_dump(mode="json", by_alias=True)["arguments"]
        if specialized_arguments is None or serialized_arguments != specialized_arguments:
            return False
    try:
        argument_paths = _argument_leaf_paths(candidate.arguments)
    except ValueError:
        return False
    if not _required_activity_arguments_are_valid(candidate.skill_name, candidate.arguments):
        return False
    if hint is None:
        return not argument_paths

    allowed_argument_paths = {field.path for field in hint.allowed_args}
    required_argument_paths = {field.path for field in hint.missing_args}
    if not argument_paths.issubset(allowed_argument_paths):
        return False
    return all(_contains_path(argument_paths, required) for required in required_argument_paths)


def select_gateway_v2_action(
    context: GatewayV2AgentContext,
    candidates: Sequence[GatewayV2AgentAction],
) -> GatewayV2AgentAction:
    allowed_actions = set(context.allowed_decision_actions)
    for candidate in candidates:
        if isinstance(candidate, GatewayV2CallSkillAction):
            candidate = _normalize_gateway_v2_action(context, candidate)
            if isinstance(candidate, GatewayV2CallSkillAction):
                if _skill_is_permitted(context, candidate):
                    return candidate
                continue
            if candidate.action in allowed_actions:
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


def _snapshot_value(snapshot: Mapping[str, Any], canonical: str, legacy: str) -> Any:
    return snapshot.get(canonical, snapshot.get(legacy))


def _initial_room_transition_action(
    context: GatewayV2AgentContext,
) -> GatewayV2CallSkillAction | None:
    if context.lease_kind != "observation":
        return None

    snapshot = context.session_snapshot
    scene_id = _snapshot_value(snapshot, "SceneId", "sceneId")
    scene_name = _snapshot_value(snapshot, "SceneName", "sceneName")
    navigation_available = _snapshot_value(
        snapshot,
        "NavigationAvailable",
        "navigationAvailable",
    )
    skill_executing = _snapshot_value(snapshot, "SkillExecuting", "skillExecuting")
    last_skill_name = _snapshot_value(snapshot, "LastSkillName", "lastSkillName")
    if (
        str(scene_id) != "1"
        or not isinstance(scene_name, str)
        or scene_name.casefold() != "lobby"
        or navigation_available not in (False, "false", "False", 0)
        or skill_executing in (True, "true", "True", 1)
        or (
            isinstance(last_skill_name, str)
            and last_skill_name.casefold() == "scene_tornado"
        )
    ):
        return None

    tornado_skill = next(
        (skill for skill in context.available_skills if skill.skill_name == "scene_tornado"),
        None,
    )
    if tornado_skill is None:
        return None
    candidate = GatewayV2CallSkillAction(
        action="call_skill",
        skillName=tornado_skill.skill_name,
        schemaVersion=tornado_skill.schema_version,
        arguments={},
        reason="Move the autonomously hosted role from the initial room to the plaza",
    )
    return candidate if _skill_is_permitted(context, candidate) else None


def _planned_activity_action(
    context: GatewayV2AgentContext,
    activity_context: ActivityPlanContext,
    *,
    scene_catalog: SceneCatalog | None = None,
) -> GatewayV2AgentAction | None:
    step = activity_context.plan.current_step()
    if step.skill_name is None:
        if "wait" in context.allowed_decision_actions:
            return GatewayV2WaitAction(
                reason="Remain available for the next opportunity in the activity plan",
                waitMs=1_000,
            )
        if "no_op" in context.allowed_decision_actions:
            return GatewayV2NoOpAction(
                reason="Remain available for the next opportunity in the activity plan"
            )
        return None

    assert step.schema_version is not None
    return _gateway_v2_activity_skill_action(
        context,
        step.skill_name,
        step.schema_version,
        reason=step.intent,
        scene_catalog=scene_catalog,
        scene_target_id=step.scene_target_id,
    )


def _defer_unresolvable_activity_step(
    context: GatewayV2AgentContext,
) -> GatewayV2AgentAction | None:
    reason = "Current activity step cannot be resolved from trusted scene data"
    if "wait" in context.allowed_decision_actions:
        return GatewayV2WaitAction(reason=reason, waitMs=1_000)
    if "no_op" in context.allowed_decision_actions:
        return GatewayV2NoOpAction(reason=reason)
    if "stop_hosting" in context.allowed_decision_actions:
        return GatewayV2StopHostingAction(reason=reason)
    return None


def _decision_deadline_fallback(context: GatewayV2AgentContext) -> GatewayV2AgentAction | None:
    reason = "Decision event exceeded the internal response target"
    if "wait" in context.allowed_decision_actions:
        return GatewayV2WaitAction(reason=reason, waitMs=_staggered_wait_ms(context))
    if "no_op" in context.allowed_decision_actions:
        return GatewayV2NoOpAction(reason=reason)
    return None


def _stable_context_bucket(context: GatewayV2AgentContext) -> int:
    account_id = _snapshot_value(context.session_snapshot, "AccountId", "accountId")
    identity = account_id if isinstance(account_id, str) and account_id.strip() else context.session_id
    material = (
        f"{identity}|{context.session_id}|{context.control_generation}|"
        f"{context.state_version}"
    )
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


def _staggered_wait_ms(context: GatewayV2AgentContext) -> int:
    return 5_000 + _stable_context_bucket(context) % 25_001


def _lease_authorized_local_activity_action(
    context: GatewayV2AgentContext,
    *,
    reason: str,
    capacity_snapshot: ActivityCapacitySnapshot | None = None,
    scene_catalog: SceneCatalog | None = None,
    plan_version: int = 1,
    excluded_skill_names: frozenset[str] = frozenset(),
) -> GatewayV2CallSkillAction | None:
    if context.lease_kind != "observation" or "call_skill" not in context.allowed_decision_actions:
        return None

    published = {
        (skill.skill_name, skill.schema_version)
        for skill in context.available_skills
    }
    last_skill_name = _snapshot_value(
        context.session_snapshot,
        "LastSkillName",
        "lastSkillName",
    )
    candidates = [
        (skill_name, schema_version)
        for skill_name in (
            (*_LOCAL_FALLBACK_SKILL_ORDER, "move_to")
            if capacity_snapshot is not None
            else _LOCAL_FALLBACK_SKILL_ORDER
        )
        for schema_version in ("v1",)
        if (skill_name, schema_version) in published
        and skill_name not in excluded_skill_names
        and (capacity_snapshot is None or capacity_snapshot.is_available(skill_name))
        and not (
            isinstance(last_skill_name, str)
            and skill_name.casefold() == last_skill_name.casefold()
        )
    ]
    if not candidates:
        return None

    if capacity_snapshot is None:
        offset = _stable_context_bucket(context) % len(candidates)
        ordered = candidates[offset:] + candidates[:offset]
    else:
        identity = str(
            _snapshot_value(context.session_snapshot, "AccountId", "accountId")
            or context.session_id
        )
        weighted = capacity_snapshot.weighted_order(
            [skill_name for skill_name, _ in candidates],
            identity=identity,
            plan_version=plan_version,
        )
        ordered = [(skill_name, "v1") for skill_name in weighted]
    for skill_name, schema_version in ordered:
        scene_target_id = None
        if skill_name == "move_to":
            if scene_catalog is None:
                continue
            scene_id = scene_id_from_snapshot(context.session_snapshot)
            if scene_id is None:
                continue
            identity = str(
                _snapshot_value(context.session_snapshot, "AccountId", "accountId")
                or context.session_id
            )
            targets = scene_catalog.select_candidates(
                scene_id=scene_id,
                role_identity=identity,
                plan_version=plan_version,
                limit=1,
            )
            if not targets:
                continue
            scene_target_id = targets[0].target_id
        action = _gateway_v2_activity_skill_action(
            context,
            skill_name,
            schema_version,
            reason=reason,
            scene_catalog=scene_catalog,
            scene_target_id=scene_target_id,
        )
        if action is not None:
            return action
    return None


def _forced_test_skill_action(
    event: ClaimedGatewayEvent,
    context: GatewayV2AgentContext,
    force_skills: tuple[str, ...],
) -> GatewayV2AgentAction:
    start_index = 0
    if isinstance(event.event, SkillFinishedEvent) and event.event.payload.skill_name in force_skills:
        completed_index = force_skills.index(event.event.payload.skill_name)
        start_index = completed_index + (1 if event.event.payload.status == "success" else 0)
    else:
        last_skill_name = _snapshot_value(context.session_snapshot, "LastSkillName", "lastSkillName")
        if isinstance(last_skill_name, str) and last_skill_name in force_skills:
            start_index = force_skills.index(last_skill_name) + 1

    for offset in range(len(force_skills)):
        skill_name = force_skills[(start_index + offset) % len(force_skills)]
        action = _gateway_v2_activity_skill_action(
            context,
            skill_name,
            "v1",
            reason="Forced by LLM_GATEWAY_V2_FORCE_SKILLS for Gateway testing",
        )
        if action is not None:
            return action

    reason = "None of the configured test skills is authorized by the current Gateway lease"
    if "wait" in context.allowed_decision_actions:
        return GatewayV2WaitAction(reason=reason, waitMs=1_000)
    if "no_op" in context.allowed_decision_actions:
        return GatewayV2NoOpAction(reason=reason)
    if "stop_hosting" in context.allowed_decision_actions:
        return GatewayV2StopHostingAction(reason=reason)
    raise GatewayV2DecisionSelectionError


def _gateway_v2_activity_skill_action(
    context: GatewayV2AgentContext,
    skill_name: str,
    schema_version: str,
    *,
    reason: str,
    scene_catalog: SceneCatalog | None = None,
    scene_target_id: str | None = None,
    arguments_override: Mapping[str, Any] | None = None,
) -> GatewayV2CallSkillAction | None:
    hint = _skill_argument_hint(context, skill_name, schema_version)
    arguments: Mapping[str, Any]
    if arguments_override is not None:
        arguments = arguments_override
    elif skill_name == "move_to" and scene_catalog is not None:
        if scene_target_id is None:
            return None
        target = scene_catalog.get_movement_target(scene_target_id)
        scene_id = scene_id_from_snapshot(context.session_snapshot)
        if target is None or scene_id is None or target.scene_id != scene_id:
            return None
        arguments = {"target": target.coordinates.as_arguments()}
    else:
        arguments = {} if hint is None else hint.suggested_args
    if skill_name in _SPECIALIZED_ARGUMENT_SKILLS:
        specialized_arguments = _specialized_arguments_for_context(
            context,
            skill_name,
            schema_version,
        )
        if specialized_arguments is None:
            return None
        arguments = specialized_arguments
    try:
        candidate = GatewayV2CallSkillAction(
            action="call_skill",
            skillName=skill_name,
            schemaVersion=schema_version,
            arguments=arguments,
            reason=reason,
        )
    except Exception:
        return None
    return candidate if _skill_is_permitted(context, candidate) else None


def gateway_v2_activity_skill_is_permitted(
    context: GatewayV2AgentContext,
    skill_name: str,
    schema_version: str,
) -> bool:
    arguments_override = (
        {"target": {"x": 0.0, "y": 0.0, "z": 0.0}}
        if skill_name == "move_to"
        else None
    )
    return (
        _gateway_v2_activity_skill_action(
            context,
            skill_name,
            schema_version,
            reason="Validate the current activity step against the Gateway lease",
            arguments_override=arguments_override,
        )
        is not None
    )


class GatewayV2DecisionService:
    def __init__(
        self,
        *,
        runner: GatewayV2AgentRunner | None = None,
        timeout_seconds: float = 30.0,
        capacity_limiter: AgentCapacityLimiter | None = None,
        metrics: GatewayV2RuntimeMetrics | None = None,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if runner is None:
            from src.core.agents.gateway_v2 import build_gateway_v2_decision_graph

            runner = cast(GatewayV2AgentRunner, build_gateway_v2_decision_graph().compile())
        self._runner: GatewayV2AgentRunner = runner
        self._timeout_seconds = timeout_seconds
        self._capacity_limiter = capacity_limiter
        self._metrics = metrics

    async def decide(
        self,
        context: GatewayV2AgentContext,
        *,
        user_id: str,
        tenant_id: str,
        activity_context: ActivityPlanContext | None = None,
    ) -> GatewayV2AgentAction:
        activity_plan = (
            None
            if activity_context is None
            else activity_context.plan.model_dump(mode="json", by_alias=True)
        )
        current_step = (
            None
            if activity_context is None
            else activity_context.plan.current_step().model_dump(mode="json", by_alias=True)
        )
        initial_state = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "snapshot": context.prompt_payload()["session"],
            "gateway_context": context.model_dump(mode="json"),
            "rag_context": "",
            "reasoned_actions": [],
            "errors": [],
            "tracking_summary": "",
            "anomalies": [],
            "abandoned_tracking_ids": [],
            "player_memory": {},
            "activity_plan": activity_plan,
            "recent_action_history": (
                [] if activity_context is None else [dict(item) for item in activity_context.recent_actions]
            ),
            "recent_failure_history": (
                [] if activity_context is None else [dict(item) for item in activity_context.recent_failures]
            ),
            "current_step": current_step,
        }
        async def invoke_runner() -> Mapping[str, Any]:
            if self._capacity_limiter is None:
                return await self._runner.ainvoke(initial_state)
            async with self._capacity_limiter.slot():
                return await self._runner.ainvoke(initial_state)

        agent_started = time.monotonic()
        agent_outcome = "cancelled"
        try:
            result = await asyncio.wait_for(invoke_runner(), timeout=self._timeout_seconds)
        except AgentCapacityExceededError as error:
            agent_outcome = "overloaded"
            raise GatewayV2AgentExecutionError("overloaded") from error
        except TimeoutError:
            agent_outcome = "timeout"
            raise GatewayV2AgentExecutionError("timeout") from None
        except Exception:
            agent_outcome = "error"
            raise GatewayV2AgentExecutionError("execution_failed") from None
        else:
            agent_outcome = "success"
        finally:
            if self._metrics is not None:
                self._metrics.record_agent_result(
                    agent_outcome,
                    elapsed_ms=(time.monotonic() - agent_started) * 1_000,
                )

        if result.get("errors"):
            raise GatewayV2AgentExecutionError("node_error")
        selected = result.get("selected_action")
        if selected is None:
            raise GatewayV2AgentExecutionError("empty_output")
        try:
            return _normalize_gateway_v2_action(
                context,
                parse_gateway_v2_agent_action(selected),
            )
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
    trace_id: str,
    context: GatewayV2AgentContext,
    action: GatewayV2AgentAction,
) -> FrozenGatewayV2Decision:
    action = _normalize_gateway_v2_action(context, action)
    if (
        isinstance(action, GatewayV2CallSkillAction)
        and action.skill_name in _SPECIALIZED_ARGUMENT_SKILLS.union(_REQUIRED_ACTIVITY_ARGUMENTS)
        and not _skill_is_permitted(context, action)
    ):
        action = _safe_unavailable_skill_action(context)
    payload: dict[str, Any] = {
        "traceId": trace_id,
        "contractVersion": "llm-gateway-http-v2",
        "sessionId": context.session_id,
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
        activity_binding: ActivityPlanBinding | None = None,
    ) -> PlannedDecision: ...


@dataclass(frozen=True)
class GatewayV2DecisionPlanner:
    decision_service: GatewayV2DecisionService
    repository: _DecisionPlanRepository
    activity_coordinator: ActivityPlanCoordinator | None = None
    scene_catalog: SceneCatalog | None = None
    force_skills: tuple[str, ...] = ()
    token_usage_tracker: GatewayV2TokenUsageTracker = gateway_v2_token_usage_tracker
    decision_target_seconds: float | None = None
    activity_capacity_repository: ActivityCapacityRepository | None = None
    now_ms: Callable[[], int] = lambda: int(time.time() * 1_000)

    def __post_init__(self) -> None:
        if self.decision_target_seconds is not None and self.decision_target_seconds <= 0:
            raise ValueError("decision_target_seconds must be positive")

    async def __call__(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> EventProcessResult:
        token_scope = self.token_usage_tracker.start_decision(
            event_id=event.event_id,
            session_id=context.session_id,
            control_generation=context.control_generation,
            decision_lease_id=context.decision_lease_id,
        )
        try:
            result = await self._plan(event, context)
        except BaseException:
            self.token_usage_tracker.complete_decision(
                token_scope,
                decision_status="unhandled_error",
            )
            raise
        self.token_usage_tracker.complete_decision(
            token_scope,
            decision_status=result.outcome,
        )
        return result

    async def _plan(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> EventProcessResult:
        try:
            existing = await self.repository.find_by_source_event(event)
            if existing is not None:
                return EventProcessResult("succeeded")

            account_id = context.session_snapshot.get("AccountId", context.session_snapshot.get("accountId"))
            if not isinstance(account_id, str) or not account_id.strip():
                return EventProcessResult(
                    "manual",
                    error_stage="agent",
                    error_category="missing_account_id",
                )
            if context.lease_kind == "conversation":
                return EventProcessResult(
                    "manual",
                    error_stage="chat",
                    error_category="conversation_lease_not_supported",
                )
            deadline_reached = (
                self.decision_target_seconds is not None
                and self.now_ms() - event.event.occurred_at_ms
                >= self.decision_target_seconds * 1_000
            )
            action: GatewayV2AgentAction | None = None
            if not deadline_reached:
                action = _initial_room_transition_action(context)
            activity_context = None
            if (
                action is not None
                and not self.force_skills
                and self.activity_coordinator is not None
            ):
                prepare_deterministic = getattr(
                    self.activity_coordinator,
                    "prepare_deterministic",
                    None,
                )
                if callable(prepare_deterministic):
                    activity_context = await prepare_deterministic(event, context)
            if (
                deadline_reached
                and action is None
                and not self.force_skills
                and self.activity_coordinator is not None
            ):
                activity_context = await self.activity_coordinator.resume(event, context)
            if (
                not deadline_reached
                and action is None
                and not self.force_skills
                and self.activity_coordinator is not None
            ):
                activity_context = await self.activity_coordinator.prepare(event, context)
            if action is None and self.force_skills:
                action = _forced_test_skill_action(event, context, self.force_skills)
            if action is None and activity_context is not None:
                planned_action = _planned_activity_action(
                    context,
                    activity_context,
                    scene_catalog=self.scene_catalog,
                )
                if planned_action is not None:
                    action = planned_action
                elif activity_context.plan.current_step().skill_name is not None:
                    action = _defer_unresolvable_activity_step(context)
                    if action is None:
                        raise GatewayV2AgentExecutionError(
                            "activity_step_unresolvable"
                        )
            if action is None and deadline_reached:
                action = _lease_authorized_local_activity_action(
                    context,
                    reason="Continue with a lease-authorized local activity after queue delay",
                )
                if action is None:
                    action = _decision_deadline_fallback(context)
                if action is None:
                    raise GatewayV2AgentExecutionError("deadline_exceeded")
                logger.warning(
                    "LLM Gateway v2 queued event exceeded decision target; planning fallback",
                    extra={
                        "event_id": event.event_id,
                        "trace_id": event.trace_id,
                        "session_id": context.session_id,
                        "control_generation": context.control_generation,
                        "decision_lease_id": context.decision_lease_id,
                        "state_version": context.state_version,
                        "event_age_ms": self.now_ms() - event.event.occurred_at_ms,
                    },
                )
            if action is None:
                try:
                    action = await self.decision_service.decide(
                        context,
                        user_id=account_id,
                        tenant_id=str(event.tenant_id),
                        activity_context=activity_context,
                    )
                except GatewayV2AgentExecutionError as error:
                    if error.category not in {"timeout", "overloaded", "deadline_exceeded"}:
                        raise
                    action = _lease_authorized_local_activity_action(
                        context,
                        reason=(
                            "Continue with a lease-authorized local activity while "
                            "model capacity is saturated"
                        ),
                    )
                    if action is None and "wait" not in context.allowed_decision_actions:
                        raise
                    logger.warning(
                        (
                            "LLM Gateway v2 Agent unavailable before decision deadline; "
                            "planning lease-authorized fallback decision"
                        ),
                        extra={
                            "event_id": event.event_id,
                            "trace_id": event.trace_id,
                            "session_id": context.session_id,
                            "control_generation": context.control_generation,
                            "decision_lease_id": context.decision_lease_id,
                            "state_version": context.state_version,
                            "error_category": error.category,
                        },
                    )
                    if action is None:
                        action = GatewayV2WaitAction(
                            reason="Agent unavailable; defer decision within the current lease",
                            waitMs=_staggered_wait_ms(context),
                        )
            elif isinstance(action, GatewayV2CallSkillAction):
                logger.info(
                    "LLM Gateway v2 deterministic skill selected",
                    extra={
                        "event_id": event.event_id,
                        "trace_id": event.trace_id,
                        "session_id": context.session_id,
                        "control_generation": context.control_generation,
                        "decision_lease_id": context.decision_lease_id,
                        "state_version": context.state_version,
                        "skill_name": action.skill_name,
                    },
                )
            if activity_context is not None and isinstance(action, GatewayV2CallSkillAction):
                current_step = activity_context.plan.current_step()
                if current_step.skill_name != action.skill_name:
                    if "wait" in context.allowed_decision_actions:
                        action = GatewayV2WaitAction(
                            reason="Current activity step is not authorized by this lease",
                            waitMs=1_000,
                        )
                    elif "no_op" in context.allowed_decision_actions:
                        action = GatewayV2NoOpAction(
                            reason="Current activity step is not authorized by this lease"
                        )
            if action is None:
                raise GatewayV2AgentExecutionError("empty_output")
            action = _normalize_gateway_v2_action(context, action)
            try:
                if activity_context is None:
                    await self.repository.plan_decision(event, context, action)
                else:
                    await self.repository.plan_decision(
                        event,
                        context,
                        action,
                        activity_binding=activity_context.binding,
                    )
            except ActivityCapacityFullError as error:
                capacity_snapshot = None
                if self.activity_capacity_repository is not None:
                    try:
                        capacity_snapshot = await self.activity_capacity_repository.snapshot(
                            event.gateway_id,
                            context,
                        )
                    except ActivityCapacityUnavailableError:
                        logger.warning(
                            "Activity capacity refresh failed after a full reservation",
                            extra={
                                "event_id": event.event_id,
                                "gateway_id": event.gateway_id,
                                "skill_name": error.skill_name,
                            },
                        )
                fallback = _lease_authorized_local_activity_action(
                    context,
                    reason=(
                        f"{error.skill_name} reached its configured capacity; "
                        "continue elsewhere"
                    ),
                    capacity_snapshot=capacity_snapshot,
                    scene_catalog=self.scene_catalog,
                    plan_version=(
                        activity_context.plan.version
                        if activity_context is not None
                        else 1
                    ),
                    excluded_skill_names=frozenset({error.skill_name}),
                )
                if fallback is None:
                    fallback = _decision_deadline_fallback(context)
                if fallback is None:
                    raise
                try:
                    await self.repository.plan_decision(event, context, fallback)
                except ActivityCapacityFullError:
                    wait = _decision_deadline_fallback(context)
                    if wait is None:
                        raise
                    await self.repository.plan_decision(event, context, wait)
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
        except GatewayV2AgentExecutionError as error:
            return EventProcessResult(
                "retryable_failed",
                error_stage=error.stage,
                error_category=error.category,
            )
