#!/bin/sh

set -eu

OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://ollama:11434}"
OLLAMA_MODELS="${OLLAMA_MODELS:-qwen3-embedding:4b,dengcao/Qwen3-Reranker-4B:Q4_K_M}"
OLLAMA_HEALTH_TIMEOUT_SECONDS="${OLLAMA_HEALTH_TIMEOUT_SECONDS:-600}"

deadline=$(( $(date +%s) + OLLAMA_HEALTH_TIMEOUT_SECONDS ))
while ! curl -fsS --connect-timeout 5 --max-time 10 "${OLLAMA_BASE_URL%/}/api/tags" >/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[ollama-init] Ollama did not become ready before timeout" >&2
        exit 1
    fi
    sleep 2
done

old_ifs=$IFS
IFS=','
set -- $OLLAMA_MODELS
IFS=$old_ifs

for model in "$@"; do
    model=$(printf '%s' "$model" | sed 's/^ *//;s/ *$//')
    if [ -z "$model" ]; then
        continue
    fi

    echo "[ollama-init] ensuring model ${model} is available"
    curl -fsS --retry 5 --retry-delay 2 \
        -X POST "${OLLAMA_BASE_URL%/}/api/pull" \
        -H 'Content-Type: application/json' \
        --data "{\"model\":\"${model}\",\"stream\":false}" \
        >/dev/null
    curl -fsS --retry 3 --retry-delay 1 \
        -X POST "${OLLAMA_BASE_URL%/}/api/show" \
        -H 'Content-Type: application/json' \
        --data "{\"name\":\"${model}\"}" \
        >/dev/null
done

echo "[ollama-init] all configured models are ready"
