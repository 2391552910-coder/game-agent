from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

ActivityCapacityScope = Literal["gateway", "scene", "scene_instance"]


@dataclass(frozen=True)
class ActivityCapacityRule:
    skill_name: str
    limit: int
    scene_id: int | None = None
    base_weight: float = 1.0
    scope: ActivityCapacityScope = "scene_instance"

    def __post_init__(self) -> None:
        if not self.skill_name.strip():
            raise ValueError("skill_name is required")
        if self.limit <= 0:
            raise ValueError("activity capacity limit must be positive")
        if self.base_weight <= 0 or not math.isfinite(self.base_weight):
            raise ValueError("activity base weight must be finite and positive")
        if self.scope not in {"gateway", "scene", "scene_instance"}:
            raise ValueError("unsupported activity capacity scope")


class ActivityCapacityPolicy:
    def __init__(
        self,
        rules: Sequence[ActivityCapacityRule],
        *,
        unrestricted_weights: Mapping[str, float] | None = None,
    ) -> None:
        by_identity: dict[tuple[str, int | None], ActivityCapacityRule] = {}
        for rule in rules:
            identity = (rule.skill_name, rule.scene_id)
            if identity in by_identity:
                raise ValueError(f"duplicate activity capacity rule: {identity}")
            by_identity[identity] = rule
        weights = dict(unrestricted_weights or {})
        for skill_name, weight in weights.items():
            if not skill_name.strip() or weight <= 0 or not math.isfinite(weight):
                raise ValueError("unrestricted activity weights must be finite and positive")
        self._rules = MappingProxyType(by_identity)
        self._unrestricted_weights = MappingProxyType(weights)

    @property
    def rules(self) -> tuple[ActivityCapacityRule, ...]:
        return tuple(self._rules.values())

    def rule_for(self, skill_name: str, *, scene_id: int | None) -> ActivityCapacityRule | None:
        return self._rules.get((skill_name, scene_id)) or self._rules.get((skill_name, None))

    def limit_for(self, skill_name: str, *, scene_id: int | None) -> int | None:
        rule = self.rule_for(skill_name, scene_id=scene_id)
        return None if rule is None else rule.limit

    def base_weight_for(self, skill_name: str, *, scene_id: int | None) -> float:
        rule = self.rule_for(skill_name, scene_id=scene_id)
        if rule is not None:
            return rule.base_weight
        return self._unrestricted_weights.get(skill_name, 1.0)

    def capacity_key(
        self,
        gateway_id: str,
        skill_name: str,
        session_snapshot: Mapping[str, Any],
    ) -> str | None:
        scene_id = scene_id_from_snapshot(session_snapshot)
        if self.rule_for(skill_name, scene_id=scene_id) is None:
            return None
        rule = self.rule_for(skill_name, scene_id=scene_id)
        assert rule is not None
        if rule.scope == "gateway":
            return f"{gateway_id}:skill:{skill_name}"
        scene_scope = "unknown" if scene_id is None else str(scene_id)
        if rule.scope == "scene":
            return f"{gateway_id}:scene:{scene_scope}:skill:{skill_name}"
        instance_id = scene_instance_id_from_snapshot(session_snapshot)
        instance_scope = instance_id or "default"
        return (
            f"{gateway_id}:scene:{scene_scope}:instance:{instance_scope}:"
            f"skill:{skill_name}"
        )

    def capacity_keys_for_snapshot(
        self,
        gateway_id: str,
        session_snapshot: Mapping[str, Any],
    ) -> tuple[str, ...]:
        keys = {
            key
            for rule in self.rules
            if (key := self.capacity_key(gateway_id, rule.skill_name, session_snapshot)) is not None
        }
        return tuple(sorted(keys))


DEFAULT_ACTIVITY_CAPACITY_POLICY = ActivityCapacityPolicy(
    (
        ActivityCapacityRule("dance_auto_schedule", 33, base_weight=0.8),
        ActivityCapacityRule("darts_auto_schedule", 16, base_weight=0.7),
        ActivityCapacityRule("shooting_auto_schedule", 25, base_weight=0.75),
        ActivityCapacityRule("seat_sit", 377, scene_id=7, base_weight=0.65),
        ActivityCapacityRule("seat_sit", 150, scene_id=8, base_weight=0.65),
        ActivityCapacityRule("hot_air_balloon_auto_schedule", 5, base_weight=0.5),
        ActivityCapacityRule("helicopter_auto_schedule", 10, base_weight=0.55),
        ActivityCapacityRule(
            "elevator_auto_schedule",
            12,
            base_weight=0.6,
            scope="gateway",
        ),
    ),
    unrestricted_weights={
        "coffee_auto_schedule": 1.5,
        "paper_plane_auto_schedule": 1.35,
        "draw_lots_auto_schedule": 1.25,
        "wish_board_auto_schedule": 1.25,
        "sign_in": 0.8,
        "move_to": 3.0,
    },
)


@dataclass(frozen=True)
class ActivityCapacitySnapshot:
    scene_id: int | None
    active_by_skill: Mapping[str, int]
    policy: ActivityCapacityPolicy = field(default=DEFAULT_ACTIVITY_CAPACITY_POLICY)

    def __post_init__(self) -> None:
        normalized: dict[str, int] = {}
        for skill_name, active in self.active_by_skill.items():
            if not skill_name.strip() or type(active) is not int or active < 0:
                raise ValueError("activity occupancy values must be non-negative integers")
            normalized[skill_name] = active
        object.__setattr__(self, "active_by_skill", MappingProxyType(normalized))

    def active_count(self, skill_name: str) -> int:
        return self.active_by_skill.get(skill_name, 0)

    def remaining(self, skill_name: str) -> int | None:
        limit = self.policy.limit_for(skill_name, scene_id=self.scene_id)
        if limit is None:
            return None
        return max(limit - self.active_count(skill_name), 0)

    def is_available(self, skill_name: str) -> bool:
        remaining = self.remaining(skill_name)
        return remaining is None or remaining > 0

    def effective_weight(self, skill_name: str) -> float:
        base_weight = self.policy.base_weight_for(skill_name, scene_id=self.scene_id)
        limit = self.policy.limit_for(skill_name, scene_id=self.scene_id)
        if limit is None:
            return base_weight
        remaining_ratio = max(1.0 - self.active_count(skill_name) / limit, 0.0)
        return base_weight * remaining_ratio * remaining_ratio

    def weighted_order(
        self,
        candidates: Sequence[str],
        *,
        identity: str,
        plan_version: int,
    ) -> tuple[str, ...]:
        unique_candidates = tuple(dict.fromkeys(candidates))
        scored: list[tuple[float, str]] = []
        for skill_name in unique_candidates:
            weight = self.effective_weight(skill_name)
            if weight <= 0:
                continue
            digest = hashlib.sha256(
                f"activity-capacity:{identity}:{plan_version}:{skill_name}".encode()
            ).digest()
            raw = int.from_bytes(digest[:8], "big")
            uniform = (raw + 1) / (2**64 + 1)
            priority = -math.log(uniform) / weight
            scored.append((priority, skill_name))
        scored.sort(key=lambda item: (item[0], item[1]))
        return tuple(skill_name for _, skill_name in scored)


def scene_id_from_snapshot(snapshot: Mapping[str, Any]) -> int | None:
    raw = snapshot.get("SceneId", snapshot.get("sceneId"))
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return None


def scene_instance_id_from_snapshot(snapshot: Mapping[str, Any]) -> str | None:
    for canonical, legacy in (
        ("SceneInstanceId", "sceneInstanceId"),
        ("SceneInstance", "sceneInstance"),
        ("InstanceId", "instanceId"),
        ("LineId", "lineId"),
    ):
        raw = snapshot.get(canonical, snapshot.get(legacy))
        if isinstance(raw, (str, int)) and not isinstance(raw, bool) and str(raw).strip():
            return str(raw).strip()
    return None
