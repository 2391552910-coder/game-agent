from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

_MOVEMENT_TARGET_KINDS = frozenset({"activity", "trigger", "navigation"})


class SceneCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class SceneCoordinates:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.x, self.y, self.z)):
            raise SceneCatalogError("scene coordinates must be finite")

    def as_arguments(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}

    def comparison_key(self) -> tuple[float, float, float]:
        return (round(self.x, 6), round(self.y, 6), round(self.z, 6))


@dataclass(frozen=True)
class SceneTarget:
    target_id: str
    scene_id: int
    scene_name: str
    kind: str
    activity: str
    point_key: str
    coordinates: SceneCoordinates
    source_path: str

    def prompt_item(self) -> dict[str, str | int]:
        return {
            "targetId": self.target_id,
            "sceneId": self.scene_id,
            "sceneName": self.scene_name,
            "kind": self.kind,
            "activity": self.activity,
        }


class SceneCatalog:
    def __init__(self, targets: Sequence[SceneTarget]) -> None:
        by_id: dict[str, SceneTarget] = {}
        by_scene: defaultdict[int, list[SceneTarget]] = defaultdict(list)
        for target in targets:
            if target.target_id in by_id:
                raise SceneCatalogError(f"duplicate scene target: {target.target_id}")
            by_id[target.target_id] = target
            by_scene[target.scene_id].append(target)
        self._by_id = MappingProxyType(by_id)
        self._by_scene = MappingProxyType(
            {
                scene_id: tuple(sorted(items, key=lambda item: item.target_id))
                for scene_id, items in by_scene.items()
            }
        )

    @classmethod
    def from_directory(cls, root: str | Path) -> SceneCatalog:
        config_root = Path(root)
        if not config_root.is_dir():
            raise SceneCatalogError(f"scene config directory does not exist: {config_root}")

        targets: list[SceneTarget] = []
        targets.extend(_load_activity_points(config_root / "RobotActivityPoint"))
        targets.extend(_load_scene_triggers(config_root / "RobotSceneAutoTrigger"))
        targets.extend(_load_shooting_tables(config_root / "RobotShootingTablePoint"))
        targets.extend(_load_navigation_points(config_root))
        if not targets:
            raise SceneCatalogError("scene config contains no supported targets")
        return cls(targets)

    def targets_for_scene(self, scene_id: int) -> tuple[SceneTarget, ...]:
        return self._by_scene.get(scene_id, ())

    def get_target(self, target_id: str) -> SceneTarget | None:
        return self._by_id.get(target_id)

    def get_movement_target(self, target_id: str) -> SceneTarget | None:
        target = self.get_target(target_id)
        if target is None or target.kind not in _MOVEMENT_TARGET_KINDS:
            return None
        return target

    def select_candidates(
        self,
        *,
        scene_id: int,
        role_identity: str,
        plan_version: int,
        limit: int = 5,
        recent_actions: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[SceneTarget, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        targets = [
            target
            for target in self.targets_for_scene(scene_id)
            if target.kind in _MOVEMENT_TARGET_KINDS
        ]
        if not targets:
            return ()

        recent_coordinates = _recent_move_coordinates(recent_actions)
        unvisited = [
            target
            for target in targets
            if target.coordinates.comparison_key() not in recent_coordinates
        ]
        if unvisited:
            targets = unvisited

        offset = (_stable_role_bucket(role_identity) + max(plan_version - 1, 0)) % len(targets)
        ordered = targets[offset:] + targets[:offset]
        return tuple(ordered[:limit])

    def prompt_candidates(
        self,
        *,
        scene_id: int,
        role_identity: str,
        plan_version: int,
        limit: int = 5,
        recent_actions: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[dict[str, str | int], ...]:
        return tuple(
            target.prompt_item()
            for target in self.select_candidates(
                scene_id=scene_id,
                role_identity=role_identity,
                plan_version=plan_version,
                limit=limit,
                recent_actions=recent_actions,
            )
        )


def scene_id_from_snapshot(snapshot: Mapping[str, Any]) -> int | None:
    raw = snapshot.get("SceneId", snapshot.get("sceneId"))
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return None


def role_identity_from_snapshot(snapshot: Mapping[str, Any], *, fallback: str) -> str:
    for canonical, legacy in (
        ("RoleId", "roleId"),
        ("AccountId", "accountId"),
        ("SessionId", "sessionId"),
    ):
        value = snapshot.get(canonical, snapshot.get(legacy))
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return fallback


@lru_cache(maxsize=1)
def load_default_scene_catalog() -> SceneCatalog:
    root = Path(__file__).resolve().parents[4] / "resources" / "scene_config"
    return SceneCatalog.from_directory(root)


def _load_json_files(directory: Path) -> list[Mapping[str, Any]]:
    if not directory.is_dir():
        return []
    documents: list[Mapping[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".report.json"):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SceneCatalogError(f"invalid scene config: {path}") from error
        if not isinstance(value, Mapping):
            raise SceneCatalogError(f"scene config root must be an object: {path}")
        documents.append(value)
    return documents


def _load_activity_points(directory: Path) -> list[SceneTarget]:
    targets: list[SceneTarget] = []
    for document in _load_json_files(directory):
        for raw in _require_list(document, "points"):
            item = _require_mapping(raw, "activity point")
            scene_id = _require_int(item, "sceneId")
            activity = _require_string(item, "activityName")
            point_key = _require_string(item, "pointKey")
            targets.append(
                SceneTarget(
                    target_id=f"scene:{scene_id}:activity:{activity}:{point_key}",
                    scene_id=scene_id,
                    scene_name=_require_string(item, "sceneName"),
                    kind="activity",
                    activity=activity,
                    point_key=point_key,
                    coordinates=_coordinates(item.get("target"), "activity target"),
                    source_path=_require_string(item, "sourcePath"),
                )
            )
    return targets


def _load_scene_triggers(directory: Path) -> list[SceneTarget]:
    targets: list[SceneTarget] = []
    for document in _load_json_files(directory):
        for raw in _require_list(document, "triggers"):
            item = _require_mapping(raw, "scene trigger")
            scene_id = _require_int(item, "sceneId")
            trigger_id = _require_int(item, "triggerId")
            action_kinds = [
                str(action.get("robotActionKind")).strip()
                for action in _require_list(item, "enterActions")
                if isinstance(action, Mapping) and str(action.get("robotActionKind", "")).strip()
            ]
            activity = action_kinds[0] if action_kinds else "scene_trigger"
            targets.append(
                SceneTarget(
                    target_id=f"scene:{scene_id}:trigger:{trigger_id}",
                    scene_id=scene_id,
                    scene_name=_require_string(document, "sceneName"),
                    kind="trigger",
                    activity=activity,
                    point_key=str(trigger_id),
                    coordinates=_coordinates(item.get("triggerPosition"), "trigger position"),
                    source_path=_require_string(item, "sourcePath"),
                )
            )
    return targets


def _load_shooting_tables(directory: Path) -> list[SceneTarget]:
    targets: list[SceneTarget] = []
    for document in _load_json_files(directory):
        for raw in _require_list(document, "tables"):
            item = _require_mapping(raw, "shooting table")
            scene_id = _require_int(item, "sceneId")
            table_num = _require_int(item, "tableNum")
            targets.append(
                SceneTarget(
                    target_id=f"scene:{scene_id}:shooting:{table_num}",
                    scene_id=scene_id,
                    scene_name=_require_string(item, "sceneName"),
                    kind="shooting",
                    activity="shooting",
                    point_key=str(table_num),
                    coordinates=_coordinates(item.get("targetPosition"), "shooting target"),
                    source_path=_require_string(item, "sourcePath"),
                )
            )
    return targets


def _load_navigation_points(config_root: Path) -> list[SceneTarget]:
    targets: list[SceneTarget] = []
    resolved_root = config_root.resolve()
    for document in _load_json_files(config_root / "RobotNavigationPoint"):
        scene_id = _require_int(document, "sceneId")
        scene_name = _require_string(document, "sceneName")
        source_relative = Path(_require_string(document, "sourceNavmesh"))
        if source_relative.is_absolute():
            raise SceneCatalogError("sourceNavmesh must be relative to scene config")
        source_path = (resolved_root / source_relative).resolve()
        try:
            source_path.relative_to(resolved_root)
        except ValueError as error:
            raise SceneCatalogError("sourceNavmesh must remain inside scene config") from error
        expected_hash = _require_string(document, "sourceNavmeshSha256").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise SceneCatalogError("sourceNavmeshSha256 must be a SHA-256 hex digest")
        if _sha256_file(source_path) != expected_hash:
            raise SceneCatalogError(f"source navmesh hash mismatch: {source_relative.as_posix()}")

        for raw in _require_list(document, "points"):
            item = _require_mapping(raw, "navigation point")
            point_key = _require_string(item, "pointKey")
            targets.append(
                SceneTarget(
                    target_id=f"scene:{scene_id}:navigation:{point_key}",
                    scene_id=scene_id,
                    scene_name=scene_name,
                    kind="navigation",
                    activity="wander",
                    point_key=point_key,
                    coordinates=_coordinates(item.get("target"), "navigation target"),
                    source_path=_require_string(item, "sourcePath"),
                )
            )
    return targets


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise SceneCatalogError(f"source navmesh cannot be read: {path}") from error
    return digest.hexdigest()


def _stable_role_bucket(identity: str) -> int:
    trailing_number = re.search(r"(\d+)$", identity.strip())
    if trailing_number is not None:
        return int(trailing_number.group(1))
    return int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")


def _recent_move_coordinates(
    recent_actions: Sequence[Mapping[str, Any]],
) -> set[tuple[float, float, float]]:
    coordinates: set[tuple[float, float, float]] = set()
    for action in recent_actions:
        body = action.get("request_body_json")
        if not isinstance(body, Mapping) or body.get("skillName") != "move_to":
            continue
        arguments = body.get("arguments")
        target = arguments.get("target") if isinstance(arguments, Mapping) else None
        try:
            coordinates.add(_coordinates(target, "recent move target").comparison_key())
        except SceneCatalogError:
            continue
    return coordinates


def _coordinates(value: object, label: str) -> SceneCoordinates:
    item = _require_mapping(value, label)
    values: list[float] = []
    for axis in ("x", "y", "z"):
        raw = item.get(axis, item.get(axis.upper()))
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise SceneCatalogError(f"{label}.{axis} must be numeric")
        values.append(float(raw))
    return SceneCoordinates(*values)


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SceneCatalogError(f"{label} must be an object")
    return value


def _require_list(value: Mapping[str, Any], key: str) -> list[Any]:
    items = value.get(key)
    if not isinstance(items, list):
        raise SceneCatalogError(f"{key} must be an array")
    return items


def _require_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise SceneCatalogError(f"{key} must be a non-empty string")
    return item.strip()


def _require_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise SceneCatalogError(f"{key} must be an integer")
    return item
