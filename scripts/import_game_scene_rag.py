"""导入游戏场景数据到 LightRAG。

默认读取:
    game_docs/数据导入文件夹

导入策略:
    - 自动扫描数据导入文件夹里的 .jsonl/.json/.txt 文件
    - JSONL: 每行作为一个独立文档
    - JSON: documents 数组里的每一项作为独立文档；普通 JSON 文件作为一个文档
    - TXT: 整个文本文件作为一个文档
    - 分批调用 rag.ainsert
    - 文档 ID 使用 doc_id，便于追踪
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from collections.abc import Iterable, Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_IMPORT_DIR = PROJECT_ROOT / "game_docs" / "数据导入文件夹"
SUPPORTED_SUFFIXES = {".jsonl", ".json", ".txt"}


@dataclass(frozen=True)
class ImportDocument:
    doc_id: str
    source_path: str
    content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import game scene RAG documents into LightRAG.")
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_IMPORT_DIR,
        help="Path to a RAG import file or a directory containing .jsonl/.json/.txt files.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="How many documents to send to LightRAG per batch.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional maximum number of documents to import. 0 means no limit.",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Optional LightRAG workspace. Use the same workspace for querying later.",
    )
    return parser.parse_args()


def discover_input_files(input_path: Path) -> list[Path]:
    """Return supported import files in deterministic order."""
    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"Unsupported input file type: {input_path}")
        return [input_path]

    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    return sorted(
        path
        for path in input_path.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _document_from_item(item: dict, fallback_doc_id: str, fallback_source_path: str) -> ImportDocument | None:
    content = (item.get("content") or "").strip()
    if not content:
        return None

    doc_id = str(item.get("doc_id") or fallback_doc_id)
    source_path = str(item.get("source_path") or fallback_source_path)
    return ImportDocument(doc_id=doc_id, source_path=source_path, content=content)


def _iter_jsonl_documents(path: Path) -> Iterator[ImportDocument]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                yield ImportDocument(
                    doc_id=f"{path.stem}-line-{line_no}",
                    source_path=str(path),
                    content=line,
                )
                continue
            doc = _document_from_item(
                item,
                fallback_doc_id=f"{path.stem}-line-{line_no}",
                fallback_source_path=str(path),
            )
            if doc is not None:
                yield doc


def _iter_json_documents(path: Path) -> Iterator[ImportDocument]:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    if isinstance(payload, dict) and isinstance(payload.get("documents"), list):
        for index, item in enumerate(payload["documents"], start=1):
            if not isinstance(item, dict):
                continue
            doc = _document_from_item(
                item,
                fallback_doc_id=f"{path.stem}-document-{index}",
                fallback_source_path=str(path),
            )
            if doc is not None:
                yield doc
        return

    yield ImportDocument(doc_id=path.name, source_path=str(path), content=raw.strip())


def _iter_text_documents(path: Path) -> Iterator[ImportDocument]:
    content = path.read_text(encoding="utf-8").strip()
    if content:
        yield ImportDocument(doc_id=path.name, source_path=str(path), content=content)


def iter_import_documents(files: Iterable[Path]) -> Iterator[ImportDocument]:
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            yield from _iter_jsonl_documents(path)
        elif suffix == ".json":
            yield from _iter_json_documents(path)
        elif suffix == ".txt":
            yield from _iter_text_documents(path)


async def main() -> None:
    args = parse_args()
    input_path: Path = args.input

    input_files = discover_input_files(input_path)
    if not input_files:
        raise FileNotFoundError(f"No supported import files found in: {input_path}")

    print(f"Input: {input_path}")
    print("Files:")
    for path in input_files:
        print(f"  - {path}")
    print(f"Batch size: {args.batch_size}")
    if args.limit:
        print(f"Limit: {args.limit}")

    from src.core.engine.lightrag_engine import get_rag, shutdown_rag

    rag = await get_rag(workspace=args.workspace)

    batch_contents: list[str] = []
    batch_paths: list[str] = []
    batch_ids: list[str] = []
    total = 0
    failed = 0

    async def flush_batch() -> None:
        nonlocal total, failed, batch_contents, batch_paths, batch_ids
        if not batch_contents:
            return

        try:
            await rag.ainsert(batch_contents, file_paths=batch_paths)
            total += len(batch_contents)
            print(f"Imported {total} documents")
        except Exception as exc:
            failed += len(batch_contents)
            print(f"Batch failed ({len(batch_contents)} docs): {exc}")
            for doc_id, file_path in zip(batch_ids, batch_paths, strict=False):
                print(f"  - {doc_id} | {file_path}")
        finally:
            batch_contents = []
            batch_paths = []
            batch_ids = []

    for doc in iter_import_documents(input_files):
        batch_contents.append(doc.content)
        batch_paths.append(f"{doc.doc_id}::{doc.source_path}")
        batch_ids.append(doc.doc_id)

        if len(batch_contents) >= args.batch_size:
            await flush_batch()

        if args.limit and (total + len(batch_contents)) >= args.limit:
            remaining = args.limit - total
            if remaining > 0 and len(batch_contents) > remaining:
                batch_contents = batch_contents[:remaining]
                batch_paths = batch_paths[:remaining]
                batch_ids = batch_ids[:remaining]
            await flush_batch()
            break

    await flush_batch()
    await shutdown_rag()

    print(f"Done. imported={total} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
