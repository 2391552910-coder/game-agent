from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from src.core.agents.gateway_v2_models import GatewayV2CallSkillAction
from src.core.integration.llm_gateway_v2.competitive_activity import (
    is_valid_dance_arguments,
    is_valid_darts_arguments,
    is_valid_shooting_arguments,
)
from src.core.integration.llm_gateway_v2.contracts import SkillFinishedEvent, parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.decision_service import (
    _gateway_v2_activity_skill_action,
    build_gateway_v2_agent_context,
    freeze_gateway_v2_decision,
)
from src.core.integration.llm_gateway_v2.paper_plane import is_valid_paper_plane_arguments


@dataclass(frozen=True)
class ReplayCase:
    session_id: str
    skill_name: str
    allowed_args: tuple[str, ...]
    validator: Callable[[Mapping[str, Any]], bool]


REAL_SESSION_REPLAYS = (
    ReplayCase(
        "3585057339965964288",
        "darts_auto_schedule",
        ("score", "darts", "allowPurchaseWhenInsufficient"),
        is_valid_darts_arguments,
    ),
    ReplayCase(
        "3585060191824248832",
        "dance_auto_schedule",
        ("score",),
        is_valid_dance_arguments,
    ),
    ReplayCase(
        "3585063352920178688",
        "shooting_auto_schedule",
        ("distance", "weapon", "posture", "score"),
        is_valid_shooting_arguments,
    ),
    ReplayCase(
        "3585066702994669568",
        "paper_plane_auto_schedule",
        ("planeName", "useTimeMs", "isComplete"),
        is_valid_paper_plane_arguments,
    ),
)


def _session_event(case: ReplayCase):
    allowed_args = []
    for path in case.allowed_args:
        field: dict[str, Any] = {"path": path, "type": "contract"}
        allowed_args.append(field)
    return parse_gateway_v2_event(
        {
            "eventId": f"llm-event-{case.session_id}-1-1",
            "eventType": "session_started",
            "sessionId": case.session_id,
            "controlGeneration": 1,
            "eventSequence": 1,
            "stateVersion": 1,
            "decisionLeaseId": f"lease-{case.session_id}-replay",
            "occurredAtMs": 1_786_515_000_000,
            "payload": {
                "reason": "session_running",
                "lease": {
                    "sessionId": case.session_id,
                    "controlGeneration": 1,
                    "decisionLeaseId": f"lease-{case.session_id}-replay",
                    "stateVersion": 1,
                    "leaseKind": "observation",
                    "allowedActions": ["call_skill", "wait", "no_op"],
                    "allowedSkillName": None,
                    "allowedSkillNames": [],
                    "parentSkillName": None,
                },
                "decisionContext": {
                    "session": {
                        "AccountId": f"account-{case.session_id}",
                        "SessionId": case.session_id,
                        "SceneId": 7,
                        "SceneName": "CJ_guangchang",
                        "State": 4,
                        "SkillExecuting": False,
                    },
                    "availableSkills": [
                        {
                            "SkillName": case.skill_name,
                            "SchemaVersion": "v1",
                            "RequireRunning": True,
                            "CooldownMs": 0,
                        }
                    ],
                    "skillArgumentHints": [
                        {
                            "skillName": case.skill_name,
                            "schemaVersion": "v1",
                            "argumentStatus": "ready",
                            "suggestedArgs": {},
                            "allowedArgs": allowed_args,
                            "missingArgs": [{"path": path} for path in case.allowed_args],
                            "warnings": [],
                            "nextSteps": [],
                        }
                    ],
                    "lastSkillResult": None,
                },
            },
        }
    )


def _gateway_success_terminal(
    case: ReplayCase,
    decision: Mapping[str, Any],
) -> SkillFinishedEvent:
    arguments = decision["arguments"]
    assert isinstance(arguments, Mapping)
    assert case.validator(arguments), "simulated Gateway rejected replay arguments"
    event = parse_gateway_v2_event(
        {
            "eventId": f"llm-event-{case.session_id}-1-3",
            "eventType": "skill_finished",
            "sessionId": case.session_id,
            "controlGeneration": 1,
            "eventSequence": 3,
            "stateVersion": 2,
            "decisionLeaseId": None,
            "occurredAtMs": 1_786_515_000_003,
            "payload": {
                "decisionId": decision["decisionId"],
                "skillName": case.skill_name,
                "skillCallId": f"{decision['decisionId']}-{case.skill_name}",
                "status": "success",
                "reason": "ok",
                "failureCategory": None,
                "retryable": False,
                "startedAtMs": 1_786_515_000_001,
                "finishedAtMs": 1_786_515_000_003,
            },
        }
    )
    assert isinstance(event, SkillFinishedEvent)
    return event


@pytest.mark.parametrize("case", REAL_SESSION_REPLAYS, ids=lambda case: case.skill_name)
def test_real_gateway_session_replay_requires_skill_finished_success(case: ReplayCase) -> None:
    event = _session_event(case)
    context = build_gateway_v2_agent_context(event)
    action = _gateway_v2_activity_skill_action(
        context,
        case.skill_name,
        "v1",
        reason="replay the Gateway-reported session",
    )

    assert isinstance(action, GatewayV2CallSkillAction)
    frozen = freeze_gateway_v2_decision(
        f"replay-decision-{case.session_id}",
        f"replay-trace-{case.session_id}",
        context,
        action,
    )
    assert frozen.body_json["sessionId"] == case.session_id
    assert frozen.body_json["skillName"] == case.skill_name

    terminal = _gateway_success_terminal(case, frozen.body_json)

    assert terminal.payload.status == "success"
    assert terminal.payload.reason == "ok"
    assert terminal.payload.decision_id == frozen.body_json["decisionId"]


def test_historical_dance_replay_without_score_range_emits_fixed_score_call() -> None:
    case = ReplayCase(
        "3585060191824248832",
        "dance_auto_schedule",
        ("score",),
        is_valid_dance_arguments,
    )
    context = build_gateway_v2_agent_context(_session_event(case))

    action = _gateway_v2_activity_skill_action(
        context,
        case.skill_name,
        "v1",
        reason="historical Gateway fixture without score bounds",
    )

    assert isinstance(action, GatewayV2CallSkillAction)
    assert action.arguments.keys() == {"score"}
    assert 70 <= action.arguments["score"] <= 120
