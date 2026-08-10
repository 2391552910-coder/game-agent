from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.prompts import ChatPromptTemplate

from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.integration.llm_gateway_v2.activity_plan import (
    CANONICAL_NON_CHAT_SKILLS,
    ActivityPlan,
    ActivityPlanProposal,
    ActivityPlanValidationError,
    materialize_activity_plan,
)
from src.core.integration.llm_gateway_v2.activity_plan_repository import (
    ActivityPlanContext,
    ActivityPlanRepository,
    ActivityPlanSnapshot,
)
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent
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

ActivityStepAuthorizer = Callable[[GatewayV2AgentContext, str, str], bool]

_PLAN_GENERATION_SYSTEM = """You choose a short, safe activity plan for an autonomously hosted game role.

Rules:
- Return only the structured JSON schema.
- Use only skills from the supplied availableSkills list and exact schemaVersion values.
- Never use nearby_chat_send or invent a chat, trade, combat, reward, or protocol skill.
- Use 3 to 6 executable skill steps, optionally followed by one social phase with no skillName.
- Avoid repeating a skill that just succeeded when another authorized skill is available.
- If the role is in Lobby, the first executable step must be scene_tornado:v1.
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
    def __init__(self, *, timeout_seconds: float = _PLAN_GENERATION_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

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
        }
        return await asyncio.wait_for(chain.ainvoke(values), timeout=self._timeout_seconds)


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
    ) -> None:
        self._repository = repository
        self._generator = generator
        self._plan_id_factory = plan_id_factory or (lambda: f"activity-plan-{uuid4().hex}")
        self._step_authorizer = step_authorizer or _catalog_step_is_permitted

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
) -> ActivityPlan:
    available = {
        (skill.skill_name, skill.schema_version)
        for skill in context.available_skills
        if skill.skill_name in CANONICAL_NON_CHAT_SKILLS
    }
    ordered = [
        (skill_name, "v1")
        for skill_name in _SAFE_FALLBACK_SKILL_ORDER
        if (skill_name, "v1") in available
    ]
    if len(ordered) < 3:
        raise ActivityPlanValidationError(
            "Gateway must publish at least three canonical non-chat skills for an activity plan"
        )

    lobby = _is_lobby(context.session_snapshot)
    if lobby:
        first = ("scene_tornado", "v1")
        if first not in available or not step_authorizer(context, *first):
            raise ActivityPlanValidationError(
                "Lobby activity fallback requires an authorized scene_tornado:v1"
            )
    else:
        first = next(
            (identity for identity in ordered if step_authorizer(context, *identity)),
            None,
        )
        if first is None:
            raise ActivityPlanValidationError(
                "Gateway lease does not authorize an available activity skill"
            )

    selected = [first]
    selected.extend(identity for identity in ordered if identity != first)
    selected = selected[:3]

    def phase_for(skill_name: str) -> str:
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
                    "phase": phase_for(skill_name),
                    "skillName": skill_name,
                    "schemaVersion": schema_version,
                    "intent": f"Continue the hosted activity with {skill_name}",
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
