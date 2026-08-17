from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

DART_ITEMS: tuple[str, ...] = ("general", "elementary", "advanced")
DANCE_SCORE_MIN = 70
DANCE_SCORE_MAX = 120
SHOOTING_PROJECTS: tuple[tuple[str, str, str], ...] = (
    ("10m", "pistol", "standing"),
    ("10m", "rifle", "standing"),
    ("25m", "pistol", "standing"),
    ("50m", "rifle", "standing"),
    ("50m", "rifle", "crouching"),
    ("50m", "rifle", "prone"),
)

_CORRECTABLE_FAILURE_REASONS: dict[str, frozenset[str]] = {
    "darts_auto_schedule": frozenset(
        {
            "darts_score_invalid",
            "darts_score_exceeds_limit",
            "darts_dart_plan_invalid",
            "darts_dart_item_invalid",
            "darts_dart_count_invalid",
            "darts_dart_pos_invalid",
            "darts_score_below_limit",
        }
    ),
    "dance_auto_schedule": frozenset(
        {
            "dance_score_invalid",
            "dance_score_exceeds_limit",
            "dance_score_below_limit",
        }
    ),
    "shooting_auto_schedule": frozenset(
        {
            "shooting_distance_invalid",
            "shooting_weapon_invalid",
            "shooting_posture_invalid",
            "shooting_score_invalid",
            "shooting_score_exceeds_limit",
            "shooting_project_invalid",
            "shooting_game_mode_invalid",
            "shooting_table_num_invalid",
            "shooting_score_below_limit",
        }
    ),
    "paper_plane_auto_schedule": frozenset(
        {
            "paper_plane_plane_name_invalid",
            "paper_plane_use_time_ms_invalid",
            "paper_plane_use_time_ms_out_of_range",
        }
    ),
}


def competitive_activity_seed(
    *,
    skill_name: str,
    session_id: str,
    event_id: str,
    account_id: str | None,
    control_generation: int,
    event_sequence: int,
    decision_lease_id: str,
    state_version: int,
) -> str:
    values = (
        skill_name,
        session_id,
        event_id,
        account_id or "",
        str(control_generation),
        str(event_sequence),
        decision_lease_id,
        str(state_version),
    )
    return "competitive-activity:" + ":".join(values)


def _digest(seed: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _stable_int(digest: bytes, *, offset: int, minimum: int, maximum: int) -> int:
    if type(minimum) is not int or type(maximum) is not int or minimum > maximum:
        raise ValueError("invalid score range")
    value = int.from_bytes(digest[offset : offset + 8], "big")
    return minimum + value % (maximum - minimum + 1)


def build_darts_arguments(*, seed: str) -> dict[str, Any]:
    digest = _digest(seed)
    first = digest[8] % 10
    second = first + digest[9] % (10 - first)
    counts = (first, second - first, 9 - second)
    return {
        "score": _stable_int(digest, offset=0, minimum=1, maximum=50),
        "darts": [
            {"dartItem": dart_item, "count": count}
            for dart_item, count in zip(DART_ITEMS, counts, strict=True)
        ],
        "allowPurchaseWhenInsufficient": False,
    }


def build_shooting_arguments(*, seed: str) -> dict[str, Any]:
    digest = _digest(seed)
    distance, weapon, posture = SHOOTING_PROJECTS[digest[0] % len(SHOOTING_PROJECTS)]
    return {
        "distance": distance,
        "weapon": weapon,
        "posture": posture,
        "score": _stable_int(digest, offset=1, minimum=30, maximum=80),
    }


def build_dance_arguments(
    *,
    seed: str,
) -> dict[str, Any]:
    return {
        "score": _stable_int(
            _digest(seed),
            offset=0,
            minimum=DANCE_SCORE_MIN,
            maximum=DANCE_SCORE_MAX,
        )
    }


def is_valid_darts_arguments(value: Mapping[str, Any]) -> bool:
    if set(value) != {"score", "darts", "allowPurchaseWhenInsufficient"}:
        return False
    score = value["score"]
    if type(score) is not int or not 1 <= score <= 50:
        return False
    if value["allowPurchaseWhenInsufficient"] is not False:
        return False
    darts = value["darts"]
    if not isinstance(darts, (list, tuple)) or len(darts) != len(DART_ITEMS):
        return False
    total = 0
    for item, expected_name in zip(darts, DART_ITEMS, strict=True):
        if not isinstance(item, Mapping) or set(item) != {"dartItem", "count"}:
            return False
        count = item["count"]
        if item["dartItem"] != expected_name or type(count) is not int or not 0 <= count <= 9:
            return False
        total += count
    return total == 9


def is_valid_shooting_arguments(value: Mapping[str, Any]) -> bool:
    if set(value) != {"distance", "weapon", "posture", "score"}:
        return False
    project = (value["distance"], value["weapon"], value["posture"])
    score = value["score"]
    return project in SHOOTING_PROJECTS and type(score) is int and 30 <= score <= 80


def is_valid_dance_arguments(
    value: Mapping[str, Any],
) -> bool:
    if set(value) != {"score"}:
        return False
    score = value["score"]
    return type(score) is int and DANCE_SCORE_MIN <= score <= DANCE_SCORE_MAX


def is_correctable_skill_failure(skill_name: str, reason: str) -> bool:
    return reason in _CORRECTABLE_FAILURE_REASONS.get(skill_name, ())
