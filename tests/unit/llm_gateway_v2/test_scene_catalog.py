from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _build_scene_config(root: Path) -> None:
    _write_json(
        root / "RobotActivityPoint" / "7.json",
        {
            "version": 1,
            "sceneId": 7,
            "sceneName": "CJ_guangchang",
            "points": [
                {
                    "sceneId": 7,
                    "sceneName": "CJ_guangchang",
                    "activityName": "wish_board",
                    "pointKey": str(index),
                    "sourceType": "scene_trigger",
                    "sourceId": 450 + index,
                    "sourcePath": f"wish-board-{index}",
                    "target": {"x": float(index), "y": 1.0, "z": float(index * 10)},
                    "standTransform": None,
                    "exitPosition": None,
                }
                for index in range(10)
            ],
        },
    )
    _write_json(
        root / "RobotSceneAutoTrigger" / "7.json",
        {
            "version": 1,
            "sceneId": 7,
            "sceneName": "CJ_guangchang",
            "triggers": [
                {
                    "sceneId": 7,
                    "triggerId": 175,
                    "triggerConfigId": 9,
                    "sourcePath": "plaza-big-jump",
                    "triggerPosition": {"x": 64.25, "y": -1.62, "z": 178.58},
                    "shape": {"shapeType": "box"},
                    "enterActions": [{"robotActionKind": "big_jump"}],
                    "exitActions": [],
                }
            ],
        },
    )
    _write_json(
        root / "RobotShootingTablePoint" / "8.json",
        {
            "version": 1,
            "sceneId": 8,
            "sceneName": "CJ_JiuBa_Zhong_suo",
            "tables": [
                {
                    "sceneId": 8,
                    "sceneName": "CJ_JiuBa_Zhong_suo",
                    "tableNum": 1,
                    "gameDistance": 1,
                    "sourcePath": "shooting-table-1",
                    "targetPosition": {"x": -8.261, "y": 0.052, "z": 31.04},
                }
            ],
        },
    )


def test_scene_catalog_loads_supported_structured_scene_sources(tmp_path: Path) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    config_root = tmp_path / "scene_config"
    _build_scene_config(config_root)

    catalog = scene_catalog.SceneCatalog.from_directory(config_root)

    plaza = catalog.targets_for_scene(7)
    bar = catalog.targets_for_scene(8)
    assert len(plaza) == 11
    assert len(bar) == 1
    assert plaza[0].scene_name == "CJ_guangchang"
    assert {target.kind for target in plaza} == {"activity", "trigger"}
    assert bar[0].target_id == "scene:8:shooting:1"
    assert bar[0].coordinates.as_arguments() == {"x": -8.261, "y": 0.052, "z": 31.04}


def test_shooting_table_points_are_not_generic_movement_targets(
    tmp_path: Path,
) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    config_root = tmp_path / "scene_config"
    _build_scene_config(config_root)
    catalog = scene_catalog.SceneCatalog.from_directory(config_root)

    assert catalog.get_target("scene:8:shooting:1") is not None
    assert catalog.select_candidates(
        scene_id=8,
        role_identity="role-1",
        plan_version=1,
    ) == ()
    assert catalog.get_movement_target("scene:8:shooting:1") is None
    assert catalog.get_movement_target("scene:7:activity:wish_board:0") is not None


def test_scene_candidates_are_limited_stable_and_diverse_per_role(tmp_path: Path) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    config_root = tmp_path / "scene_config"
    _build_scene_config(config_root)
    catalog = scene_catalog.SceneCatalog.from_directory(config_root)

    first_pass = catalog.select_candidates(
        scene_id=7,
        role_identity="role-3",
        plan_version=1,
        limit=5,
    )
    second_pass = catalog.select_candidates(
        scene_id=7,
        role_identity="role-3",
        plan_version=1,
        limit=5,
    )
    role_first_targets = {
        catalog.select_candidates(
            scene_id=7,
            role_identity=f"role-{index}",
            plan_version=1,
            limit=1,
        )[0].target_id
        for index in range(10)
    }

    assert first_pass == second_pass
    assert len(first_pass) == 5
    assert len(role_first_targets) == 10


def test_scene_candidates_exclude_recent_move_targets_when_alternatives_exist(
    tmp_path: Path,
) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    config_root = tmp_path / "scene_config"
    _build_scene_config(config_root)
    catalog = scene_catalog.SceneCatalog.from_directory(config_root)
    first = catalog.select_candidates(
        scene_id=7,
        role_identity="role-1",
        plan_version=1,
        limit=1,
    )[0]

    next_candidates = catalog.select_candidates(
        scene_id=7,
        role_identity="role-1",
        plan_version=2,
        limit=5,
        recent_actions=(
            {
                "request_body_json": {
                    "action": "call_skill",
                    "skillName": "move_to",
                    "arguments": {"target": first.coordinates.as_arguments()},
                }
            },
        ),
    )

    assert first.target_id not in {candidate.target_id for candidate in next_candidates}


def test_prompt_candidates_do_not_include_the_entire_scene(tmp_path: Path) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    config_root = tmp_path / "scene_config"
    _build_scene_config(config_root)
    catalog = scene_catalog.SceneCatalog.from_directory(config_root)

    prompt_items = catalog.prompt_candidates(
        scene_id=7,
        role_identity="role-7",
        plan_version=1,
        limit=5,
    )

    assert len(prompt_items) == 5
    assert all(set(item) == {"targetId", "sceneId", "sceneName", "kind", "activity"} for item in prompt_items)
    assert all("coordinates" not in item for item in prompt_items)


def test_navigation_points_are_loaded_only_when_source_navmesh_hash_matches(
    tmp_path: Path,
) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    config_root = tmp_path / "scene_config"
    _build_scene_config(config_root)
    navmesh = config_root / "RecastDot" / "CJ_JiuBa_Ce_suo"
    navmesh.parent.mkdir(parents=True, exist_ok=True)
    navmesh.write_bytes(b"trusted-navmesh-fixture")
    _write_json(
        config_root / "RobotNavigationPoint" / "9.json",
        {
            "version": 1,
            "sceneId": 9,
            "sceneName": "CJ_JiuBa_Ce_suo",
            "sourceNavmesh": "RecastDot/CJ_JiuBa_Ce_suo",
            "sourceNavmeshSha256": hashlib.sha256(navmesh.read_bytes()).hexdigest(),
            "points": [
                {
                    "pointKey": "wander-001",
                    "sourcePath": "recast:CJ_JiuBa_Ce_suo#wander-001",
                    "target": {"x": -277.861, "y": 0.011, "z": -29.233},
                }
            ],
        },
    )

    catalog = scene_catalog.SceneCatalog.from_directory(config_root)

    target = catalog.get_target("scene:9:navigation:wander-001")
    assert target is not None
    assert target.kind == "navigation"
    assert target.activity == "wander"
    assert target.coordinates.as_arguments() == {
        "x": -277.861,
        "y": 0.011,
        "z": -29.233,
    }

    navmesh.write_bytes(b"changed-navmesh")
    with pytest.raises(scene_catalog.SceneCatalogError, match="source navmesh hash mismatch"):
        scene_catalog.SceneCatalog.from_directory(config_root)


def test_packaged_scene_catalog_contains_copied_sgai_targets() -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )

    catalog = scene_catalog.load_default_scene_catalog()
    wish_board = catalog.get_target("scene:7:activity:wish_board:458")

    assert wish_board is not None
    assert wish_board.scene_name == "CJ_guangchang"
    assert wish_board.coordinates.as_arguments() == {
        "x": 100.519966,
        "y": 1.15435553,
        "z": -25.9959488,
    }
    assert len(catalog.targets_for_scene(7)) == 29
    assert len(catalog.targets_for_scene(8)) == 56


def test_packaged_scene_catalog_contains_recast_validated_bar_side_targets() -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )

    catalog = scene_catalog.load_default_scene_catalog()
    targets = catalog.targets_for_scene(9)

    assert len(targets) >= 10
    assert all(target.scene_name == "CJ_JiuBa_Ce_suo" for target in targets)
    assert all(target.kind == "navigation" for target in targets)
    assert all(target.activity == "wander" for target in targets)


@pytest.mark.parametrize(
    ("scene_id", "expected_unique_targets"),
    [
        (7, 10),
        (8, 10),
        (9, 10),
    ],
)
def test_ten_roles_receive_diverse_targets_from_packaged_scene_data(
    scene_id: int,
    expected_unique_targets: int,
) -> None:
    scene_catalog = importlib.import_module(
        "src.core.integration.llm_gateway_v2.scene_catalog"
    )
    catalog = scene_catalog.load_default_scene_catalog()

    selected = [
        catalog.select_candidates(
            scene_id=scene_id,
            role_identity=f"role-{index}",
            plan_version=1,
            limit=1,
        )[0]
        for index in range(10)
    ]

    assert len({target.target_id for target in selected}) == expected_unique_targets
    assert len({target.coordinates.comparison_key() for target in selected}) == expected_unique_targets
    assert all(target.scene_id == scene_id for target in selected)
