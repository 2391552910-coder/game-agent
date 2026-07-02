import json
from pathlib import Path

from scripts import import_game_scene_rag


def test_discover_input_files_includes_supported_files_in_stable_order(tmp_path: Path) -> None:
    (tmp_path / "b.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ignore.bin").write_bytes(b"binary")

    files = import_game_scene_rag.discover_input_files(tmp_path)

    assert [path.name for path in files] == ["a.txt", "b.jsonl", "manifest.json"]


def test_iter_import_documents_loads_json_jsonl_and_text(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "docs.jsonl"
    jsonl_path.write_text(
        json.dumps({"doc_id": "jsonl-1", "source_path": "a.xlsx", "content": "jsonl content"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    json_path = tmp_path / "docs.json"
    json_path.write_text(
        json.dumps(
            {
                "documents": [
                    {"doc_id": "json-1", "source_path": "b.xlsx", "content": "json content"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    text_path = tmp_path / "docs.txt"
    text_path.write_text("plain text content", encoding="utf-8")

    docs = list(import_game_scene_rag.iter_import_documents([json_path, jsonl_path, text_path]))

    assert [(doc.doc_id, doc.source_path, doc.content) for doc in docs] == [
        ("json-1", "b.xlsx", "json content"),
        ("jsonl-1", "a.xlsx", "jsonl content"),
        ("docs.txt", str(text_path), "plain text content"),
    ]


def test_iter_import_documents_splits_markdown_into_semantic_chunks(tmp_path: Path) -> None:
    markdown_path = tmp_path / "游戏场景数据.md"
    markdown_path.write_text(
        "\n".join(
            [
                "# myAgent2 游戏真实模拟数据集",
                "- 体育馆正门：坐标 (138, 0, 78)，建筑 ID 为 `sports_01`。",
                (
                    "- 射箭竞技活动（ID: `act_archery_02`）：位于体育馆 (138,0,78)。"
                    "参与条件玩家等级≥LV5，无需金币消耗，每次消耗15体力值，单局耗时约3分钟。"
                    "产出敏捷属性经验、80竞技积分。"
                ),
                "- 原味咖啡：15 金币（ID: `item_coffee_01`）。",
            ]
        ),
        encoding="utf-8",
    )

    files = import_game_scene_rag.discover_input_files(tmp_path)
    docs = list(import_game_scene_rag.iter_import_documents(files))

    contents = [doc.content for doc in docs]
    assert any("【map_coordinate】" in content and "体育馆正门" in content for content in contents)
    assert any("【activity_rule】" in content and "act_archery_02" in content for content in contents)
    assert any("【item_price】" in content and "item_coffee_01" in content for content in contents)
