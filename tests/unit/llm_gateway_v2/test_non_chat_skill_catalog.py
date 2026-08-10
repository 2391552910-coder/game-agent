import pytest

from src.core.agents.gateway_v2_models import GatewayV2AgentContext, GatewayV2CallSkillAction
from src.core.integration.llm_gateway_v2.decision_service import (
    freeze_gateway_v2_decision,
    select_gateway_v2_action,
)

NON_CHAT_SKILLS = (
    "observe_state",
    "move_to",
    "stop_move",
    "jump",
    "play_action",
    "scene_tornado",
    "sign_in",
    "shooting_auto_schedule",
    "darts_auto_schedule",
    "dance_auto_schedule",
    "draw_lots_auto_schedule",
    "wish_board_auto_schedule",
    "paper_plane_auto_schedule",
    "coffee_auto_schedule",
    "seat_sit",
    "seat_get_out",
    "hot_air_balloon_auto_schedule",
    "hot_air_balloon_exit",
    "helicopter_auto_schedule",
    "helicopter_exit",
    "elevator_auto_schedule",
)


def _arguments(skill_name: str) -> dict:
    if skill_name == "move_to":
        return {"target": {"x": 1, "y": 2, "z": 3}}
    if skill_name == "play_action":
        return {"actionId": "wave"}
    return {}


def _argument_paths(skill_name: str) -> list[str]:
    if skill_name == "move_to":
        return ["target.x", "target.y", "target.z"]
    if skill_name == "play_action":
        return ["actionId"]
    return []


def _invalid_arguments(skill_name: str) -> dict:
    return {**_arguments(skill_name), "unexpected": True}


def _context(
    skill_name: str,
    *,
    lease_kind: str = "observation",
    parent_skill_name: str | None = None,
    allowed_skill_names: list[str] | None = None,
) -> GatewayV2AgentContext:
    permitted_names = allowed_skill_names or [skill_name]
    return GatewayV2AgentContext.model_validate(
        {
            "event_id": "event-catalog",
            "session_id": "session-catalog",
            "control_generation": 1,
            "event_sequence": 1,
            "decision_lease_id": "lease-catalog",
            "state_version": 1,
            "lease_kind": lease_kind,
            "allowed_decision_actions": ["call_skill", "wait"],
            "parent_skill_name": parent_skill_name,
            "allowed_skill_name": None if len(permitted_names) > 1 else skill_name,
            "allowed_skill_names": permitted_names,
            "session_snapshot": {"accountId": "account-catalog"},
            "available_skills": [
                {
                    "SkillName": name,
                    "SchemaVersion": "v1",
                    "RequireRunning": True,
                    "CooldownMs": 0,
                }
                for name in permitted_names
            ],
            "skill_argument_hints": [
                {
                    "skillName": name,
                    "schemaVersion": "v1",
                    "argumentStatus": "ready",
                    "suggestedArgs": {},
                    "allowedArgs": [
                        {"path": path}
                        for path in _argument_paths(name)
                    ],
                    "missingArgs": [
                        {"path": path}
                        for path in _argument_paths(name)
                    ],
                    "warnings": [],
                    "nextSteps": [],
                }
                for name in permitted_names
            ],
            "terminal_result": None,
        }
    )


@pytest.mark.parametrize("skill_name", NON_CHAT_SKILLS)
def test_each_gateway_non_chat_skill_builds_a_v2_call_skill_decision(
    skill_name: str,
) -> None:
    action = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": skill_name,
            "schemaVersion": "v1",
            "arguments": _arguments(skill_name),
            "reason": "contract verification",
        }
    )

    wrong_schema = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": skill_name,
            "schemaVersion": "v2",
            "arguments": _arguments(skill_name),
            "reason": "wrong schema",
        }
    )
    out_of_scope_arguments = GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": skill_name,
            "schemaVersion": "v1",
            "arguments": _invalid_arguments(skill_name),
            "reason": "out of scope arguments",
        }
    )
    vehicle_parent_by_exit = {
        "hot_air_balloon_exit": "hot_air_balloon_auto_schedule",
        "helicopter_exit": "helicopter_auto_schedule",
    }
    parent_skill_name = vehicle_parent_by_exit.get(skill_name)
    context = _context(
        skill_name,
        lease_kind="vehicle_cancel_window" if parent_skill_name is not None else "observation",
        parent_skill_name=parent_skill_name,
    )

    selected = select_gateway_v2_action(
        context,
        [wrong_schema, out_of_scope_arguments, action],
    )
    frozen = freeze_gateway_v2_decision(
        f"decision-{skill_name}",
        "trace-catalog-1",
        context,
        selected,
    )

    assert frozen.body_json["skillName"] == skill_name
    assert frozen.body_json["schemaVersion"] == "v1"
    assert frozen.body_json["arguments"] == _arguments(skill_name)


@pytest.mark.parametrize("lease_kind", ["vehicle_cancel_window", "vehicle_recovery"])
@pytest.mark.parametrize(
    ("parent_skill_name", "exit_skill_name", "other_exit"),
    [
        (
            "hot_air_balloon_auto_schedule",
            "hot_air_balloon_exit",
            "helicopter_exit",
        ),
        (
            "helicopter_auto_schedule",
            "helicopter_exit",
            "hot_air_balloon_exit",
        ),
    ],
)
def test_vehicle_lease_catalog_selects_only_paired_exit(
    lease_kind: str,
    parent_skill_name: str,
    exit_skill_name: str,
    other_exit: str,
) -> None:
    context = _context(
        exit_skill_name,
        lease_kind=lease_kind,
        parent_skill_name=parent_skill_name,
        allowed_skill_names=[exit_skill_name, other_exit],
    )
    selected = select_gateway_v2_action(
        context,
        [
            GatewayV2CallSkillAction.model_validate(
                {
                    "action": "call_skill",
                    "skillName": other_exit,
                    "schemaVersion": "v1",
                    "arguments": {},
                    "reason": "wrong vehicle exit",
                }
            ),
            GatewayV2CallSkillAction.model_validate(
                {
                    "action": "call_skill",
                    "skillName": exit_skill_name,
                    "schemaVersion": "v1",
                    "arguments": {},
                    "reason": "paired vehicle exit",
                }
            ),
        ],
    )

    assert isinstance(selected, GatewayV2CallSkillAction)
    assert selected.skill_name == exit_skill_name
