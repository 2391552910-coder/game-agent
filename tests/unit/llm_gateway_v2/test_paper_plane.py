from __future__ import annotations

from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event
from src.core.integration.llm_gateway_v2.decision_service import (
    build_gateway_v2_agent_context,
    freeze_gateway_v2_decision,
)
from src.core.integration.llm_gateway_v2.paper_plane import (
    PAPER_PLANE_DURATION_RANGES_MS,
    PAPER_PLANE_NAMES,
    build_paper_plane_arguments,
)


def _context():
    event = parse_gateway_v2_event(
        {
            "eventId": "event-paper-plane",
            "eventType": "observation_updated",
            "sessionId": "session-paper-plane",
            "controlGeneration": 1,
            "eventSequence": 1,
            "stateVersion": 1,
            "decisionLeaseId": "lease-paper-plane",
            "occurredAtMs": 1_700_000_000_000,
            "payload": {
                "reason": "decision_requested",
                "lease": {
                    "sessionId": "session-paper-plane",
                    "controlGeneration": 1,
                    "decisionLeaseId": "lease-paper-plane",
                    "stateVersion": 1,
                    "leaseKind": "observation",
                    "allowedActions": ["call_skill", "wait"],
                    "allowedSkillName": "paper_plane_auto_schedule",
                    "allowedSkillNames": ["paper_plane_auto_schedule"],
                    "parentSkillName": None,
                },
                "decisionContext": {
                    "session": {"AccountId": "role-paper-plane"},
                    "availableSkills": [
                        {
                            "SkillName": "paper_plane_auto_schedule",
                            "SchemaVersion": "v1",
                            "RequireRunning": True,
                            "CooldownMs": 0,
                        }
                    ],
                    "skillArgumentHints": [
                        {
                            "skillName": "paper_plane_auto_schedule",
                            "schemaVersion": "v1",
                            "argumentStatus": "ready",
                            "suggestedArgs": {},
                            "allowedArgs": [
                                {"path": "planeName"},
                                {"path": "useTimeMs"},
                                {"path": "isComplete"},
                            ],
                            "missingArgs": [
                                {"path": "planeName"},
                                {"path": "useTimeMs"},
                                {"path": "isComplete"},
                            ],
                            "warnings": [],
                            "nextSteps": [],
                        }
                    ],
                    "lastSkillResult": None,
                },
            },
        }
    )
    return build_gateway_v2_agent_context(event)


def test_paper_plane_arguments_use_only_gateway_names_and_millisecond_ranges() -> None:
    arguments = [
        build_paper_plane_arguments(seed=f"role-{index}")
        for index in range(100)
    ]

    assert {item["planeName"] for item in arguments} == set(PAPER_PLANE_NAMES)
    assert all(set(item) == {"planeName", "useTimeMs", "isComplete"} for item in arguments)
    assert all(isinstance(item["useTimeMs"], int) for item in arguments)
    assert all(item["isComplete"] is True for item in arguments)
    for item in arguments:
        minimum, maximum = PAPER_PLANE_DURATION_RANGES_MS[item["planeName"]]
        assert minimum <= item["useTimeMs"] <= maximum


def test_paper_plane_arguments_are_stable_for_the_same_event_seed() -> None:
    first = build_paper_plane_arguments(seed="session-1:event-2:generation-3:lease-1:state-7")
    second = build_paper_plane_arguments(seed="session-1:event-2:generation-3:lease-1:state-7")

    assert first == second


def test_paper_plane_arguments_never_reuse_legacy_name_or_second_value() -> None:
    arguments = build_paper_plane_arguments(seed="session-1:event-2")

    assert arguments["planeName"] != "纸飞机A"
    assert arguments["useTimeMs"] >= 70_000


def test_freezing_a_decision_also_enforces_paper_plane_arguments() -> None:
    from src.core.agents.gateway_v2_models import GatewayV2CallSkillAction

    frozen = freeze_gateway_v2_decision(
        "decision-paper-plane",
        "trace-paper-plane",
        _context(),
        GatewayV2CallSkillAction(
            action="call_skill",
            skillName="paper_plane_auto_schedule",
            schemaVersion="v1",
            arguments={"planeName": "纸飞机A", "useTimeMs": 12, "isComplete": False},
        ),
    )

    assert frozen.body_json["arguments"]["planeName"] in PAPER_PLANE_NAMES
    minimum, maximum = PAPER_PLANE_DURATION_RANGES_MS[frozen.body_json["arguments"]["planeName"]]
    assert minimum <= frozen.body_json["arguments"]["useTimeMs"] <= maximum
    assert frozen.body_json["arguments"]["isComplete"] is True
