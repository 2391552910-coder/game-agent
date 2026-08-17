from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.prompts import ChatPromptTemplate

from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.integration.llm_gateway_v2.activity_plan import (
    CANONICAL_NON_CHAT_SKILLS,
    ActivityPlan,
    ActivityPlanProposal,
    ActivityPlanProposalStep,
    ActivityPlanValidationError,
    materialize_activity_plan,
)
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanContext,
    ActivityPlanRepository,
    ActivityPlanSnapshot,
)
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent
from src.core.integration.llm_gateway_v2.scene_catalog import (
    SceneCatalog,
    role_identity_from_snapshot,
    scene_id_from_snapshot,
)
from src.core.integration.llm_gateway_v2.token_usage import (
    gateway_v2_token_callback_config,
)
from src.core.llm.factory import get_llm

logger = logging.getLogger(__name__)

_PLAN_GENERATION_TIMEOUT_SECONDS = 20

_SAFE_FALLBACK_SKILL_ORDER: tuple[str, ...] = (
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
    "seat_sit",
    "sign_in",
    "jump",
    "observe_state",
    "play_action",
    "move_to",
    "stop_move",
    "hot_air_balloon_exit",
    "helicopter_exit",
    "scene_tornado",
    "seat_get_out",
)

_ROLE_DIVERSITY_SKILL_ORDER: tuple[str, ...] = (
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
    "seat_sit",
    "sign_in",
    "play_action",
    "jump",
)

ActivityStepAuthorizer = Callable[[GatewayV2AgentContext, str, str], bool]

_PLAN_GENERATION_SYSTEM = """You choose a short, safe activity plan for an autonomously hosted game role.

Rules:
- Return only the structured JSON schema.
- Use only skills from the supplied availableSkills list and exact schemaVersion values.
- Never use nearby_chat_send or invent a chat, trade, combat, reward, or protocol skill.
- Use 3 to 6 executable skill steps, optionally followed by one social phase with no skillName.
- Avoid repeating a skill that just succeeded when another authorized skill is available.
- If the role is in Lobby, the first executable step must be scene_tornado:v1.
- When sceneCandidates are supplied, a move_to step must select one exact sceneTargetId.
- Never invent a sceneTargetId or coordinates; sceneTargetId must come from sceneCandidates.
- Follow the supplied roleProfile so simultaneous hosted roles do not all select the same activity order.
- Each step must have a short phase and intent.
"""

_PLAN_GENERATION_USER = """Current Gateway session snapshot:
{session_snapshot}

Current available skills:
{available_skills}

Recent actions:
{recent_actions}

Recent failures:
{recent_failures}

Role profile:
{role_profile}

Current scene candidates (select by sceneTargetId; do not copy coordinates into the plan):
{scene_candidates}

Return one activity plan proposal.
"""


class ActivityPlanProposalGenerator(Protocol):
    async def generate(
        self,
        context: GatewayV2AgentContext,
        *,
        recent_actions: tuple[Mapping[str, Any], ...],
        recent_failures: tuple[Mapping[str, Any], ...],
    ) -> ActivityPlanProposal: ...


class GatewayV2ActivityPlanGenerator:
    def __init__(
        self,
        *,
        timeout_seconds: float = _PLAN_GENERATION_TIMEOUT_SECONDS,
        scene_catalog: SceneCatalog | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._scene_catalog = scene_catalog

    async def generate(
        self,
        context: GatewayV2AgentContext,
        *,
        recent_actions: tuple[Mapping[str, Any], ...],
        recent_failures: tuple[Mapping[str, Any], ...],
    ) -> ActivityPlanProposal:
        prompt = ChatPromptTemplate.from_messages(
            [("system", _PLAN_GENERATION_SYSTEM), ("human", _PLAN_GENERATION_USER)]
        )
        llm = await get_llm(model_type="default")
        structured_llm = llm.with_structured_output(ActivityPlanProposal, method="json_mode")
        chain = prompt | structured_llm
        values = {
            "session_snapshot": json.dumps(context.prompt_payload()["session"], ensure_ascii=False),
            "available_skills": json.dumps(
                [skill.model_dump(mode="json", by_alias=True) for skill in context.available_skills],
                ensure_ascii=False,
            ),
            "recent_actions": json.dumps(list(recent_actions), ensure_ascii=False, default=str),
            "recent_failures": json.dumps(list(recent_failures), ensure_ascii=False, default=str),
            "role_profile": json.dumps(
                role_profile_for_context(context),
                ensure_ascii=False,
            ),
            "scene_candidates": json.dumps(
                _scene_candidates_for_context(
                    context,
                    scene_catalog=self._scene_catalog,
                    plan_version=1,
                    recent_actions=recent_actions,
                ),
                ensure_ascii=False,
            ),
        }
        callback_config = gateway_v2_token_callback_config()
        invocation = (
            chain.ainvoke(values)
            if callback_config is None
            else chain.ainvoke(values, config=callback_config)
        )
        generated = await asyncio.wait_for(invocation, timeout=self._timeout_seconds)
        return ActivityPlanProposal.model_validate(generated)


class ActivityPlanRepositoryProtocol(Protocol):
    async def load(self, event: ClaimedGatewayEvent) -> ActivityPlanSnapshot: ...

    async def prepare(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
        *,
        proposed_plan: ActivityPlan | None = None,
    ) -> ActivityPlanContext: ...


class ActivityPlanCoordinator:
    def __init__(
        self,
        *,
        repository: ActivityPlanRepositoryProtocol | ActivityPlanRepository,
        generator: ActivityPlanProposalGenerator,
        plan_id_factory: Callable[[], str] | None = None,
        step_authorizer: ActivityStepAuthorizer | None = None,
        scene_catalog: SceneCatalog | None = None,
    ) -> None:
        self._repository = repository
        self._generator = generator
        self._plan_id_factory = plan_id_factory or (lambda: f"activity-plan-{uuid4().hex}")
        self._step_authorizer = step_authorizer or _catalog_step_is_permitted
        self._scene_catalog = scene_catalog

    async def prepare(
        self,
        event: ClaimedGatewayEvent,
        context: GatewayV2AgentContext,
    ) -> ActivityPlanContext | None:
        snapshot = await self._repository.load(event)
        if (
            snapshot.plan is not None
            and snapshot.plan.status == "active"
            and _current_step_is_executable(
                snapshot.plan,
                context,
                self._step_authorizer,
            )
            and _current_step_is_resolvable(
                snapshot.plan,
                context,
                self._scene_catalog,
            )
        ):
            return await self._repository.prepare(event, context, proposed_plan=None)

        version = max(
            snapshot.version,
            0 if snapshot.plan is None else snapshot.plan.version,
        ) + 1
        plan_id = self._plan_id_factory()
        available_skills = {
            (skill.skill_name, skill.schema_version) for skill in context.available_skills
        }
        try:
            proposal = await self._generator.generate(
                context,
                recent_actions=snapshot.recent_actions,
                recent_failures=snapshot.recent_failures,
            )
            proposal = diversify_activity_plan_proposal(
                proposal,
                context,
                scene_catalog=self._scene_catalog,
                plan_version=version,
                recent_actions=snapshot.recent_actions,
                step_authorizer=self._step_authorizer,
            )
            plan = materialize_activity_plan(
                proposal,
                plan_id=plan_id,
                version=version,
                available_skills=available_skills,
                lobby=_is_lobby(context.session_snapshot),
            )
            if not _current_step_is_executable(plan, context, self._step_authorizer):
                raise ActivityPlanValidationError(
                    "activity plan current step is not authorized by the decision lease"
                )
            if not _current_step_is_resolvable(plan, context, self._scene_catalog):
                raise ActivityPlanValidationError(
                    "activity plan current scene target is not available"
                )
        except Exception as error:
            logger.warning(
                "Activity plan generation failed; using safe fallback",
                extra={"error_type": type(error).__name__, "plan_id": plan_id},
            )
            try:
                plan = _safe_fallback_plan(
                    context,
                    plan_id,
                    version,
                    step_authorizer=self._step_authorizer,
                    scene_catalog=self._scene_catalog,
                    recent_actions=snapshot.recent_actions,
                )
            except ActivityPlanValidationError as fallback_error:
                logger.warning(
                    "Activity plan deferred because the current Gateway catalog cannot form a safe plan",
                    extra={
                        "error_type": type(fallback_error).__name__,
                        "plan_id": plan_id,
                        "available_skill_count": len(context.available_skills),
                    },
                )
                return None
        return await self._repository.prepare(event, context, proposed_plan=plan)


def _catalog_step_is_permitted(
    context: GatewayV2AgentContext,
    skill_name: str,
    schema_version: str,
) -> bool:
    if "call_skill" not in context.allowed_decision_actions:
        return False
    return any(
        (skill.skill_name, skill.schema_version) == (skill_name, schema_version)
        for skill in context.available_skills
    )


def _current_step_is_executable(
    plan: ActivityPlan,
    context: GatewayV2AgentContext,
    step_authorizer: ActivityStepAuthorizer,
) -> bool:
    step = plan.current_step()
    if step.skill_name is None:
        return True
    assert step.schema_version is not None
    return step_authorizer(context, step.skill_name, step.schema_version)


def _current_step_is_resolvable(
    plan: ActivityPlan,
    context: GatewayV2AgentContext,
    scene_catalog: SceneCatalog | None,
) -> bool:
    step = plan.current_step()
    if step.skill_name != "move_to" or scene_catalog is None:
        return True
    scene_id = scene_id_from_snapshot(context.session_snapshot)
    if scene_id is None or step.scene_target_id is None:
        return False
    target = scene_catalog.get_movement_target(step.scene_target_id)
    return target is not None and target.scene_id == scene_id


def _is_lobby(snapshot: Mapping[str, Any]) -> bool:
    scene_id = snapshot.get("SceneId", snapshot.get("sceneId"))
    scene_name = snapshot.get("SceneName", snapshot.get("sceneName"))
    navigation_available = snapshot.get("NavigationAvailable", snapshot.get("navigationAvailable"))
    return (
        str(scene_id) == "1"
        and isinstance(scene_name, str)
        and scene_name.casefold() == "lobby"
        and navigation_available in (False, "false", "False", 0)
    )


def _safe_fallback_plan(
    context: GatewayV2AgentContext,
    plan_id: str,
    version: int,
    *,
    step_authorizer: ActivityStepAuthorizer,
    scene_catalog: SceneCatalog | None = None,
    recent_actions: tuple[Mapping[str, Any], ...] = (),
) -> ActivityPlan:
    lobby = _is_lobby(context.session_snapshot)
    available = {
        (skill.skill_name, skill.schema_version)
        for skill in context.available_skills
        if skill.skill_name in CANONICAL_NON_CHAT_SKILLS
    }
    scene_targets = _scene_candidates_for_context(
        context,
        scene_catalog=scene_catalog,
        plan_version=version,
        recent_actions=recent_actions,
        limit=1,
    )
    target_id = scene_targets[0]["targetId"] if scene_targets else None
    if scene_catalog is not None and target_id is None:
        available.discard(("move_to", "v1"))
    ordered = [
        (skill_name, "v1")
        for skill_name in _SAFE_FALLBACK_SKILL_ORDER
        if (skill_name, "v1") in available
        and (lobby or skill_name != "scene_tornado")
    ]
    if len(ordered) < 3:
        raise ActivityPlanValidationError(
            "Gateway must publish at least three canonical non-chat skills for an activity plan"
        )

    role_identity = role_identity_from_snapshot(
        context.session_snapshot,
        fallback=context.session_id,
    )
    if lobby:
        first = ("scene_tornado", "v1")
        if first not in available or not step_authorizer(context, *first):
            raise ActivityPlanValidationError(
                "Lobby activity fallback requires an authorized scene_tornado:v1"
            )
    else:
        authorized_first_steps = [
            identity for identity in ordered if step_authorizer(context, *identity)
        ]
        if not authorized_first_steps:
            raise ActivityPlanValidationError(
                "Gateway lease does not authorize an available activity skill"
            )
        first = authorized_first_steps[
            _stable_rotation(role_identity, len(authorized_first_steps))
        ]
    remaining = [identity for identity in ordered if identity != first]
    if first != ("move_to", "v1") and ("move_to", "v1") in remaining:
        remaining.remove(("move_to", "v1"))
        rotation = _stable_rotation(role_identity, len(remaining) + 1)
        remaining.insert(rotation, ("move_to", "v1"))
    else:
        rotation = _stable_rotation(role_identity, len(remaining)) if remaining else 0
        remaining = remaining[rotation:] + remaining[:rotation]
    selected = [first, *remaining[:2]]
    if ("move_to", "v1") in ordered and ("move_to", "v1") not in selected:
        selected[-1] = ("move_to", "v1")

    def step_id_for(index: int, skill_name: str) -> str:
        stable_ids = {
            "scene_tornado": "arrival",
            "dance_auto_schedule": "dance",
            "hot_air_balloon_auto_schedule": "balloon",
        }
        return stable_ids.get(skill_name, f"fallback-{index + 1}-{skill_name}")

    proposal = ActivityPlanProposal.model_validate(
        {
            "goalId": "plaza_social",
            "goalSummary": "Complete a short sequence using only skills published by the Gateway",
            "steps": [
                {
                    "stepId": step_id_for(index, skill_name),
                    "phase": _phase_for_skill(skill_name),
                    "skillName": skill_name,
                    "schemaVersion": schema_version,
                    "intent": (
                        "Walk to a different authorized scene point"
                        if skill_name == "move_to"
                        else f"Continue the hosted activity with {skill_name}"
                    ),
                    "sceneTargetId": target_id if skill_name == "move_to" else None,
                }
                for index, (skill_name, schema_version) in enumerate(selected)
            ]
            + [
                {
                    "stepId": "social-opportunity",
                    "phase": "social",
                    "intent": "Remain available for a nearby friend chat opportunity",
                }
            ],
        }
    )
    return materialize_activity_plan(
        proposal,
        plan_id=plan_id,
        version=version,
        available_skills=available,
        lobby=lobby,
    )


def role_profile_for_context(context: GatewayV2AgentContext) -> dict[str, Any]:
    role_identity = role_identity_from_snapshot(
        context.session_snapshot,
        fallback=context.session_id,
    )
    bucket = _stable_role_bucket(role_identity)
    profiles = (
        ("explorer", "varied", "prefer a new scene point before repeating an activity"),
        ("social", "balanced", "prefer social and plaza activities with occasional movement"),
        ("active", "fast", "prefer movement and transport before waiting"),
        ("leisure", "slow", "prefer relaxed activities and longer transitions"),
    )
    profile_id, pace, preference = profiles[bucket % len(profiles)]
    return {
        "profileId": profile_id,
        "profileSeed": bucket,
        "pace": pace,
        "preference": preference,
        "activityRotation": bucket,
    }


def diversify_activity_plan_proposal(
    proposal: ActivityPlanProposal,
    context: GatewayV2AgentContext,
    *,
    scene_catalog: SceneCatalog | None,
    plan_version: int,
    recent_actions: tuple[Mapping[str, Any], ...],
    step_authorizer: ActivityStepAuthorizer,
) -> ActivityPlanProposal:
    if scene_catalog is None:
        return proposal

    role_identity = role_identity_from_snapshot(
        context.session_snapshot,
        fallback=context.session_id,
    )
    lobby = _is_lobby(context.session_snapshot)
    forced = [step for step in proposal.steps if lobby and step.skill_name == "scene_tornado"]
    others = [
        step
        for step in proposal.steps
        if step not in forced
        and step.skill_name is not None
        and (lobby or step.skill_name != "scene_tornado")
    ]
    social = [step for step in proposal.steps if step.skill_name is None]
    scene_candidates = _scene_candidates_for_context(
        context,
        scene_catalog=scene_catalog,
        plan_version=plan_version,
        recent_actions=recent_actions,
        limit=1,
    )
    target_id = scene_candidates[0]["targetId"] if scene_candidates else None
    if target_id is None:
        others = [step for step in others if step.skill_name != "move_to"]

    preferred = _role_preferred_activity_step(
        proposal,
        context,
        role_identity=role_identity,
        plan_version=plan_version,
        step_authorizer=step_authorizer,
    )
    if preferred is not None:
        existing_preferred = next(
            (step for step in others if step.skill_name == preferred.skill_name),
            None,
        )
        preferred = existing_preferred or preferred
        if existing_preferred is None:
            others = _append_or_replace_activity_step(
                others,
                preferred,
                reserved_skill_names={"move_to"},
                forced_count=len(forced),
            )

    move_identity = ("move_to", "v1")
    available = {(skill.skill_name, skill.schema_version) for skill in context.available_skills}
    if (
        target_id is not None
        and move_identity in available
        and step_authorizer(context, *move_identity)
        and not any(step.skill_name == "move_to" for step in others)
    ):
        move_step = proposal.steps[0].model_copy(
            update={
                "step_id": f"wander-{plan_version}-{_stable_role_bucket(role_identity) % 100000}",
                "phase": "movement",
                "skill_name": "move_to",
                "schema_version": "v1",
                "scene_target_id": None,
                "intent": "Walk to a different point in the current scene",
            }
        )
        others = _append_or_replace_activity_step(
            others,
            move_step,
            reserved_skill_names=(
                set() if preferred is None or preferred.skill_name is None else {preferred.skill_name}
            ),
            forced_count=len(forced),
        )

    if preferred is not None:
        preferred_in_plan = next(
            (step for step in others if step.skill_name == preferred.skill_name),
            None,
        )
    else:
        preferred_in_plan = None
    if preferred_in_plan is not None:
        remaining = [step for step in others if step is not preferred_in_plan]
        rotation = _stable_rotation(role_identity, len(remaining)) if remaining else 0
        others = [preferred_in_plan, *remaining[rotation:], *remaining[:rotation]]
    else:
        rotation = _stable_rotation(role_identity, len(others)) if others else 0
        others = others[rotation:] + others[:rotation]
    if not lobby:
        others = _avoid_immediate_successful_skill_repeat(
            others,
            recent_skill_name=_most_recent_successful_skill_name(recent_actions),
        )
    normalized: list[Any] = []
    for step in [*forced, *others, *social]:
        if step.skill_name == "move_to":
            normalized.append(step.model_copy(update={"scene_target_id": step.scene_target_id or target_id}))
        else:
            normalized.append(step)

    if not normalized:
        return proposal
    return proposal.model_copy(update={"steps": tuple(normalized)})


def _most_recent_successful_skill_name(
    recent_actions: tuple[Mapping[str, Any], ...],
) -> str | None:
    for action in recent_actions:
        if action.get("skill_status") != "succeeded":
            continue
        skill_name = action.get("skill_name")
        if isinstance(skill_name, str) and skill_name:
            return skill_name
        request_body = action.get("request_body_json")
        if isinstance(request_body, Mapping):
            skill_name = request_body.get("skillName")
            if isinstance(skill_name, str) and skill_name:
                return skill_name
    return None


def _avoid_immediate_successful_skill_repeat(
    steps: list[ActivityPlanProposalStep],
    *,
    recent_skill_name: str | None,
) -> list[ActivityPlanProposalStep]:
    if (
        recent_skill_name is None
        or len(steps) < 2
        or steps[0].skill_name != recent_skill_name
    ):
        return steps
    alternative_index = next(
        (
            index
            for index, step in enumerate(steps[1:], start=1)
            if step.skill_name != recent_skill_name
        ),
        None,
    )
    if alternative_index is None:
        return steps
    return [
        steps[alternative_index],
        *steps[:alternative_index],
        *steps[alternative_index + 1 :],
    ]


def _role_preferred_activity_step(
    proposal: ActivityPlanProposal,
    context: GatewayV2AgentContext,
    *,
    role_identity: str,
    plan_version: int,
    step_authorizer: ActivityStepAuthorizer,
) -> ActivityPlanProposalStep | None:
    available = {(skill.skill_name, skill.schema_version) for skill in context.available_skills}
    candidates = [
        skill_name
        for skill_name in _ROLE_DIVERSITY_SKILL_ORDER
        if (skill_name, "v1") in available
        and step_authorizer(context, skill_name, "v1")
    ]
    if not candidates:
        return None
    bucket = _stable_role_bucket(role_identity)
    skill_name = candidates[(bucket + max(plan_version - 1, 0)) % len(candidates)]
    return proposal.steps[0].model_copy(
        update={
            "step_id": f"role-{plan_version}-{bucket % 100000}-{skill_name}",
            "phase": _phase_for_skill(skill_name),
            "skill_name": skill_name,
            "schema_version": "v1",
            "scene_target_id": None,
            "intent": f"Follow this role's activity preference with {skill_name}",
        }
    )


def _append_or_replace_activity_step(
    steps: list[ActivityPlanProposalStep],
    new_step: ActivityPlanProposalStep,
    *,
    reserved_skill_names: set[str],
    forced_count: int,
) -> list[ActivityPlanProposalStep]:
    result = list(steps)
    if forced_count + len(result) < 6:
        result.append(new_step)
        return result
    replace_index = next(
        (
            index
            for index in range(len(result) - 1, -1, -1)
            if result[index].skill_name not in reserved_skill_names
        ),
        None,
    )
    if replace_index is not None:
        result[replace_index] = new_step
    return result


def _phase_for_skill(skill_name: str) -> str:
    if skill_name == "scene_tornado":
        return "arrival"
    if skill_name in {
        "hot_air_balloon_auto_schedule",
        "helicopter_auto_schedule",
        "elevator_auto_schedule",
    }:
        return "transport"
    if skill_name in {"jump", "move_to", "stop_move"}:
        return "movement"
    if skill_name in {"hot_air_balloon_exit", "helicopter_exit", "seat_get_out"}:
        return "recovery"
    return "activity"


def _scene_candidates_for_context(
    context: GatewayV2AgentContext,
    *,
    scene_catalog: SceneCatalog | None,
    plan_version: int,
    recent_actions: tuple[Mapping[str, Any], ...],
    limit: int = 5,
) -> tuple[dict[str, Any], ...]:
    if scene_catalog is None:
        return ()
    scene_id = scene_id_from_snapshot(context.session_snapshot)
    if scene_id is None:
        return ()
    role_identity = role_identity_from_snapshot(
        context.session_snapshot,
        fallback=context.session_id,
    )
    return scene_catalog.prompt_candidates(
        scene_id=scene_id,
        role_identity=role_identity,
        plan_version=plan_version,
        limit=limit,
        recent_actions=recent_actions,
    )


def _stable_role_bucket(identity: str) -> int:
    match = re.search(r"(\d+)$", identity.strip())
    if match is not None:
        return int(match.group(1))
    return sum((index + 1) * ord(char) for index, char in enumerate(identity))


def _stable_rotation(identity: str, size: int) -> int:
    return 0 if size <= 0 else _stable_role_bucket(identity) % size
