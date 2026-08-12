from __future__ import annotations

import pytest

from src.core.integration.llm_gateway_v2.competitive_activity import (
    SHOOTING_PROJECTS,
    build_dance_arguments,
    build_darts_arguments,
    build_shooting_arguments,
    is_correctable_skill_failure,
    is_valid_dance_arguments,
    is_valid_darts_arguments,
    is_valid_shooting_arguments,
)


def test_darts_arguments_are_stable_and_match_gateway_contract() -> None:
    arguments = build_darts_arguments(seed="darts:session-1:event-1:lease-1")

    assert arguments == build_darts_arguments(seed="darts:session-1:event-1:lease-1")
    assert set(arguments) == {"score", "darts", "allowPurchaseWhenInsufficient"}
    assert 1 <= arguments["score"] <= 50
    assert arguments["allowPurchaseWhenInsufficient"] is False
    assert [item["dartItem"] for item in arguments["darts"]] == [
        "general",
        "elementary",
        "advanced",
    ]
    assert sum(item["count"] for item in arguments["darts"]) == 9
    assert is_valid_darts_arguments(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"score": 0, "darts": [], "allowPurchaseWhenInsufficient": False},
        {
            "score": 51,
            "darts": [
                {"dartItem": "general", "count": 3},
                {"dartItem": "elementary", "count": 3},
                {"dartItem": "advanced", "count": 3},
            ],
            "allowPurchaseWhenInsufficient": False,
        },
        {
            "score": 25,
            "darts": [
                {"dartItem": "general", "count": 3},
                {"dartItem": "elementary", "count": 3},
                {"dartItem": "advanced", "count": 2},
            ],
            "allowPurchaseWhenInsufficient": False,
        },
        {
            "score": 25,
            "darts": [
                {"dartItem": "general", "count": 3},
                {"dartItem": "elementary", "count": 3},
                {"dartItem": "advanced", "count": 3},
            ],
            "allowPurchaseWhenInsufficient": True,
        },
    ],
)
def test_darts_validator_rejects_gateway_business_failures(arguments: dict[str, object]) -> None:
    assert not is_valid_darts_arguments(arguments)


def test_shooting_arguments_are_stable_and_match_gateway_contract() -> None:
    arguments = build_shooting_arguments(seed="shooting:session-1:event-1:lease-1")

    assert arguments == build_shooting_arguments(seed="shooting:session-1:event-1:lease-1")
    assert set(arguments) == {"distance", "weapon", "posture", "score"}
    assert (
        arguments["distance"],
        arguments["weapon"],
        arguments["posture"],
    ) in SHOOTING_PROJECTS
    assert 30 <= arguments["score"] <= 80
    assert is_valid_shooting_arguments(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"distance": "25m", "weapon": "rifle", "posture": "standing", "score": 50},
        {"distance": "10m", "weapon": "pistol", "posture": "standing", "score": 29},
        {"distance": "50m", "weapon": "rifle", "posture": "prone", "score": 81},
        {
            "distance": "10m",
            "weapon": "pistol",
            "posture": "standing",
            "score": 50,
            "tableNum": 3,
        },
    ],
)
def test_shooting_validator_rejects_invalid_or_noncanonical_arguments(
    arguments: dict[str, object],
) -> None:
    assert not is_valid_shooting_arguments(arguments)


def test_dance_arguments_require_and_obey_gateway_supplied_score_range() -> None:
    arguments = build_dance_arguments(seed="dance:session-1:event-1:lease-1", minimum=12, maximum=27)

    assert arguments == build_dance_arguments(
        seed="dance:session-1:event-1:lease-1",
        minimum=12,
        maximum=27,
    )
    assert set(arguments) == {"score"}
    assert 12 <= arguments["score"] <= 27
    assert is_valid_dance_arguments(arguments, minimum=12, maximum=27)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(None, 50), (1, None), (50, 1), (True, 50), (1.5, 50)],
)
def test_dance_arguments_reject_missing_or_invalid_gateway_range(
    minimum: object,
    maximum: object,
) -> None:
    with pytest.raises(ValueError, match="score range"):
        build_dance_arguments(
            seed="dance:session-1:event-1:lease-1",
            minimum=minimum,
            maximum=maximum,
        )


@pytest.mark.parametrize(
    ("skill_name", "reason"),
    [
        ("darts_auto_schedule", "darts_score_invalid"),
        ("darts_auto_schedule", "darts_score_exceeds_limit"),
        ("dance_auto_schedule", "dance_score_invalid"),
        ("dance_auto_schedule", "dance_score_exceeds_limit"),
        ("shooting_auto_schedule", "shooting_distance_invalid"),
        ("shooting_auto_schedule", "shooting_project_invalid"),
        ("shooting_auto_schedule", "shooting_score_exceeds_limit"),
        ("paper_plane_auto_schedule", "paper_plane_use_time_invalid"),
    ],
)
def test_known_parameter_failures_allow_a_corrected_decision(
    skill_name: str,
    reason: str,
) -> None:
    assert is_correctable_skill_failure(skill_name, reason)


@pytest.mark.parametrize(
    ("skill_name", "reason"),
    [
        ("darts_auto_schedule", "purchase_failed"),
        ("shooting_auto_schedule", "session_not_running"),
        ("coffee_auto_schedule", "coffee_name_invalid"),
    ],
)
def test_non_parameter_failures_do_not_retry_under_a_new_lease(
    skill_name: str,
    reason: str,
) -> None:
    assert not is_correctable_skill_failure(skill_name, reason)
