#!/bin/sh

set -eu

input_path="${RAG_IMPORT_INPUT:-/app/game_docs/数据导入文件夹}"
batch_size="${RAG_IMPORT_BATCH_SIZE:-25}"
limit="${RAG_IMPORT_LIMIT:-0}"
workspace="${RAG_IMPORT_WORKSPACE:-}"

set -- python /app/scripts/import_game_scene_rag.py \
    --input "$input_path" \
    --batch-size "$batch_size"

if [ "$limit" -gt 0 ]; then
    set -- "$@" --limit "$limit"
fi

if [ -n "$workspace" ]; then
    set -- "$@" --workspace "$workspace"
fi

exec "$@"
