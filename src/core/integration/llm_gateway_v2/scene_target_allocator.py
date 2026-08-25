from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import Lock

from src.core.integration.llm_gateway_v2.scene_catalog import SceneTarget


@dataclass(frozen=True)
class _WindowKey:
    gateway_id: str
    scene_id: int
    control_generation: int
    window_index: int


@dataclass
class _WindowState:
    claimed_coordinates: set[tuple[float, float, float]] = field(default_factory=set)


class SceneTargetAllocator:
    """De-duplicate trusted targets in a short local scheduling window.

    The result is only a consumer-side hint. Gateway remains authoritative for
    distance, capacity, navigation, and final execution admission.
    """

    def __init__(self, *, window_ms: int = 5_000, retention_windows: int = 2) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        if retention_windows < 1:
            raise ValueError("retention_windows must be positive")
        self._window_ms = window_ms
        self._retention_windows = retention_windows
        self._lock = Lock()
        self._windows: dict[_WindowKey, _WindowState] = {}
        self._event_results: dict[tuple[str, str, str], tuple[_WindowKey, SceneTarget | None]] = {}

    def allocate(
        self,
        *,
        gateway_id: str,
        session_id: str,
        event_id: str,
        scene_id: int,
        control_generation: int,
        occurred_at_ms: int,
        candidates: Iterable[SceneTarget],
        preferred_target_id: str | None = None,
    ) -> SceneTarget | None:
        if not gateway_id.strip() or not session_id.strip() or not event_id.strip():
            raise ValueError("gateway_id, session_id, and event_id must be non-empty")
        if scene_id < 0 or control_generation <= 0 or occurred_at_ms < 0:
            raise ValueError("scene_id, control_generation, and occurred_at_ms are invalid")

        window_key = _WindowKey(
            gateway_id=gateway_id,
            scene_id=scene_id,
            control_generation=control_generation,
            window_index=occurred_at_ms // self._window_ms,
        )
        event_key = (gateway_id, session_id, event_id)
        candidate_list = tuple(candidates)
        with self._lock:
            self._cleanup(window_key.window_index)
            previous = self._event_results.get(event_key)
            if previous is not None:
                return previous[1]

            state = self._windows.setdefault(window_key, _WindowState())
            ordered = _preferred_first(candidate_list, preferred_target_id)
            selected = next(
                (
                    target
                    for target in ordered
                    if target.coordinates.comparison_key() not in state.claimed_coordinates
                ),
                None,
            )
            if selected is not None:
                state.claimed_coordinates.add(selected.coordinates.comparison_key())
            self._event_results[event_key] = (window_key, selected)
            return selected

    def _cleanup(self, current_window_index: int) -> None:
        cutoff = current_window_index - self._retention_windows
        old_keys = [key for key in self._windows if key.window_index < cutoff]
        for key in old_keys:
            self._windows.pop(key, None)
        old_events = [
            event_key
            for event_key, (key, _) in self._event_results.items()
            if key.window_index < cutoff
        ]
        for event_key in old_events:
            self._event_results.pop(event_key, None)


def _preferred_first(
    candidates: tuple[SceneTarget, ...],
    preferred_target_id: str | None,
) -> tuple[SceneTarget, ...]:
    if preferred_target_id is None:
        return candidates
    preferred = next(
        (candidate for candidate in candidates if candidate.target_id == preferred_target_id),
        None,
    )
    if preferred is None:
        return candidates
    return (preferred, *(candidate for candidate in candidates if candidate is not preferred))
