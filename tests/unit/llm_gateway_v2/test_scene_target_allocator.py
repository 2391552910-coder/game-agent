from __future__ import annotations

from src.core.integration.llm_gateway_v2.scene_catalog import SceneCoordinates, SceneTarget
from src.core.integration.llm_gateway_v2.scene_target_allocator import SceneTargetAllocator


def _target(point_key: str, x: float) -> SceneTarget:
    return SceneTarget(
        target_id=f"scene:7:navigation:{point_key}",
        scene_id=7,
        scene_name="CJ_guangchang",
        kind="navigation",
        activity="wander",
        point_key=point_key,
        coordinates=SceneCoordinates(x, 0.0, 20.0),
        source_path="test-scene",
    )


def test_allocator_deduplicates_exact_coordinates_in_one_window() -> None:
    allocator = SceneTargetAllocator(window_ms=5_000)
    candidates = (_target("1", 1.0), _target("2", 2.0), _target("3", 3.0))

    assigned = [
        allocator.allocate(
            gateway_id="gateway-1",
            session_id=f"session-{index}",
            event_id=f"event-{index}",
            scene_id=7,
            control_generation=1,
            occurred_at_ms=10_000,
            candidates=candidates,
        )
        for index in range(3)
    ]

    assert [item.target_id for item in assigned if item is not None] == [
        "scene:7:navigation:1",
        "scene:7:navigation:2",
        "scene:7:navigation:3",
    ]
    assert allocator.allocate(
        gateway_id="gateway-1",
        session_id="session-3",
        event_id="event-3",
        scene_id=7,
        control_generation=1,
        occurred_at_ms=10_000,
        candidates=candidates,
    ) is None


def test_allocator_replays_the_same_result_for_the_same_event() -> None:
    allocator = SceneTargetAllocator(window_ms=5_000)
    candidates = (_target("1", 1.0), _target("2", 2.0))

    first = allocator.allocate(
        gateway_id="gateway-1",
        session_id="session-1",
        event_id="event-1",
        scene_id=7,
        control_generation=1,
        occurred_at_ms=10_000,
        candidates=candidates,
    )
    replay = allocator.allocate(
        gateway_id="gateway-1",
        session_id="session-1",
        event_id="event-1",
        scene_id=7,
        control_generation=1,
        occurred_at_ms=10_000,
        candidates=(candidates[1],),
    )

    assert replay == first


def test_allocator_restarts_reservation_for_new_generation_and_window() -> None:
    allocator = SceneTargetAllocator(window_ms=5_000)
    candidates = (_target("1", 1.0),)

    first = allocator.allocate(
        gateway_id="gateway-1",
        session_id="session-1",
        event_id="event-1",
        scene_id=7,
        control_generation=1,
        occurred_at_ms=10_000,
        candidates=candidates,
    )
    next_generation = allocator.allocate(
        gateway_id="gateway-1",
        session_id="session-2",
        event_id="event-2",
        scene_id=7,
        control_generation=2,
        occurred_at_ms=10_000,
        candidates=candidates,
    )
    next_window = allocator.allocate(
        gateway_id="gateway-1",
        session_id="session-3",
        event_id="event-3",
        scene_id=7,
        control_generation=1,
        occurred_at_ms=15_000,
        candidates=candidates,
    )

    assert first == next_generation == next_window == candidates[0]
