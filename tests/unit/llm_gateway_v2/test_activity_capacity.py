from __future__ import annotations

from src.core.integration.llm_gateway_v2.activity_capacity import (
    DEFAULT_ACTIVITY_CAPACITY_POLICY,
    ActivityCapacitySnapshot,
)


def test_default_capacity_policy_uses_verified_game_limits() -> None:
    policy = DEFAULT_ACTIVITY_CAPACITY_POLICY

    assert policy.limit_for("dance_auto_schedule", scene_id=8) == 33
    assert policy.limit_for("darts_auto_schedule", scene_id=8) == 16
    assert policy.limit_for("shooting_auto_schedule", scene_id=8) == 25
    assert policy.limit_for("seat_sit", scene_id=7) == 377
    assert policy.limit_for("seat_sit", scene_id=8) == 150
    assert policy.limit_for("hot_air_balloon_auto_schedule", scene_id=7) == 5
    assert policy.limit_for("helicopter_auto_schedule", scene_id=7) == 10
    assert policy.limit_for("elevator_auto_schedule", scene_id=7) == 12


def test_coffee_and_open_world_actions_are_not_capacity_limited() -> None:
    policy = DEFAULT_ACTIVITY_CAPACITY_POLICY

    assert policy.limit_for("coffee_auto_schedule", scene_id=8) is None
    assert policy.limit_for("move_to", scene_id=7) is None
    assert policy.limit_for("paper_plane_auto_schedule", scene_id=7) is None


def test_capacity_key_uses_configured_game_scope() -> None:
    policy = DEFAULT_ACTIVITY_CAPACITY_POLICY
    snapshot = {
        "SceneId": 8,
        "SceneInstanceId": "instance-3",
    }

    assert policy.capacity_key(
        "gateway-1",
        "dance_auto_schedule",
        snapshot,
    ) == "gateway-1:scene:8:instance:instance-3:skill:dance_auto_schedule"
    assert policy.capacity_key(
        "gateway-1",
        "seat_sit",
        snapshot,
    ) == "gateway-1:scene:8:instance:instance-3:skill:seat_sit"
    assert policy.capacity_key(
        "gateway-1",
        "hot_air_balloon_auto_schedule",
        snapshot,
    ) == (
        "gateway-1:scene:8:instance:instance-3:"
        "skill:hot_air_balloon_auto_schedule"
    )
    assert policy.capacity_key(
        "gateway-1",
        "elevator_auto_schedule",
        snapshot,
    ) == "gateway-1:skill:elevator_auto_schedule"


def test_per_instance_transport_capacity_is_independent() -> None:
    policy = DEFAULT_ACTIVITY_CAPACITY_POLICY

    first = policy.capacity_key(
        "gateway-1",
        "hot_air_balloon_auto_schedule",
        {"SceneId": 8, "SceneInstanceId": "instance-1"},
    )
    second = policy.capacity_key(
        "gateway-1",
        "hot_air_balloon_auto_schedule",
        {"SceneId": 8, "SceneInstanceId": "instance-2"},
    )

    assert first != second


def test_scene_instance_capacity_is_independent() -> None:
    policy = DEFAULT_ACTIVITY_CAPACITY_POLICY

    scene_eight_first = policy.capacity_key(
        "gateway-1",
        "dance_auto_schedule",
        {"SceneId": 8, "SceneInstanceId": "instance-1"},
    )
    scene_eight_second = policy.capacity_key(
        "gateway-1",
        "dance_auto_schedule",
        {"SceneId": 8, "SceneInstanceId": "instance-2"},
    )
    scene_seven = policy.capacity_key(
        "gateway-1",
        "dance_auto_schedule",
        {"SceneId": 7, "SceneInstanceId": "instance-1"},
    )

    assert scene_eight_first != scene_eight_second
    assert scene_eight_first != scene_seven


def test_saturated_activity_has_zero_effective_weight() -> None:
    snapshot = ActivityCapacitySnapshot(
        scene_id=8,
        active_by_skill={"dance_auto_schedule": 33},
    )

    assert snapshot.effective_weight("dance_auto_schedule") == 0.0
    assert snapshot.is_available("dance_auto_schedule") is False


def test_capacity_weight_falls_quadratically_as_activity_fills() -> None:
    empty = ActivityCapacitySnapshot(scene_id=8, active_by_skill={})
    half_full = ActivityCapacitySnapshot(
        scene_id=8,
        active_by_skill={"shooting_auto_schedule": 12},
    )

    empty_weight = empty.effective_weight("shooting_auto_schedule")
    half_weight = half_full.effective_weight("shooting_auto_schedule")

    assert empty_weight > half_weight > 0
    assert half_weight < empty_weight * 0.35


def test_weighted_order_is_stable_but_diverse_across_roles() -> None:
    snapshot = ActivityCapacitySnapshot(scene_id=8, active_by_skill={})
    candidates = (
        "dance_auto_schedule",
        "darts_auto_schedule",
        "shooting_auto_schedule",
        "coffee_auto_schedule",
        "paper_plane_auto_schedule",
        "move_to",
    )

    first = snapshot.weighted_order(candidates, identity="role-101", plan_version=1)
    repeated = snapshot.weighted_order(candidates, identity="role-101", plan_version=1)
    selected = {
        snapshot.weighted_order(candidates, identity=f"role-{index}", plan_version=1)[0]
        for index in range(100, 130)
    }

    assert first == repeated
    assert len(selected) >= 4


def test_weighted_order_excludes_full_activities_and_keeps_wandering() -> None:
    snapshot = ActivityCapacitySnapshot(
        scene_id=8,
        active_by_skill={
            "dance_auto_schedule": 33,
            "darts_auto_schedule": 16,
            "shooting_auto_schedule": 25,
        },
    )

    ordered = snapshot.weighted_order(
        (
            "dance_auto_schedule",
            "darts_auto_schedule",
            "shooting_auto_schedule",
            "move_to",
        ),
        identity="role-1",
        plan_version=1,
    )

    assert ordered == ("move_to",)
