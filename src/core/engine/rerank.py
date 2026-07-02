"""Rerank provider adapters used by LightRAG."""

import asyncio
import json
import math
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

_OLLAMA_HOSTS = {"localhost:11434", "127.0.0.1:11434", "[::1]:11434"}
_NUMBER_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?%?")


def is_ollama_rerank_base_url(base_url: str) -> bool:
    """Return whether the configured rerank base URL points at a local Ollama server."""
    parsed_url = urlparse(base_url)
    return parsed_url.netloc.lower() in _OLLAMA_HOSTS


def resolve_ollama_generate_endpoint(base_url: str) -> str:
    """Resolve an Ollama base URL or OpenAI-compatible URL to the native generate endpoint."""
    normalized_base_url = base_url.rstrip("/")
    if normalized_base_url.endswith("/api/generate"):
        return normalized_base_url
    if normalized_base_url.endswith("/v1"):
        normalized_base_url = normalized_base_url[: -len("/v1")]
    if normalized_base_url.endswith("/api"):
        return f"{normalized_base_url}/generate"
    return f"{normalized_base_url}/api/generate"


def _build_rerank_prompt(query: str, document: str) -> str:
    return (
        "You are a relevance scoring model. Given a query and a document, judge how useful "
        "the document is for answering the query.\n"
        "Return only one number from 0.0 to 1.0. Do not explain.\n\n"
        f"Query:\n{query}\n\n"
        f"Document:\n{document}\n\n"
        "Relevance score:"
    )


def _clamp_score(score: float) -> float:
    if math.isnan(score) or math.isinf(score):
        return 0.0
    return max(0.0, min(1.0, score))


def _parse_rerank_score(text: str) -> float:
    normalized_text = text.strip()
    lowered_text = normalized_text.lower()

    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        for key in ("score", "relevance_score", "relevance"):
            value = parsed.get(key)
            if isinstance(value, int | float):
                return _clamp_score(float(value))
            if isinstance(value, str):
                numeric_match = _NUMBER_RE.search(value)
                if numeric_match:
                    return _parse_numeric_score(numeric_match.group(0))

    numeric_match = _NUMBER_RE.search(normalized_text)
    if numeric_match:
        return _parse_numeric_score(numeric_match.group(0))

    first_token = re.sub(r"[^a-zA-Z\u4e00-\u9fff]+", " ", lowered_text).strip().split(" ")[0]
    if first_token in {"yes", "true", "relevant", "是", "相关"}:
        return 1.0
    if first_token in {"no", "false", "irrelevant", "否", "不相关"}:
        return 0.0
    return 0.0


def _parse_numeric_score(raw_score: str) -> float:
    if raw_score.endswith("%"):
        return _clamp_score(float(raw_score[:-1]) / 100)
    score = float(raw_score)
    if score > 1:
        score = score / 100
    return _clamp_score(score)


async def ollama_rerank(
    query: str,
    documents: list[str],
    top_n: int | None = None,
    api_key: str | None = None,
    model: str = "dengcao/Qwen3-Reranker-4B:Q4_K_M",
    base_url: str = "http://localhost:11434",
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    timeout: float = 120,
    max_concurrency: int = 4,
) -> list[dict[str, Any]]:
    """Rerank documents with a local Ollama reranker model."""
    if not documents or top_n == 0:
        return []

    endpoint = resolve_ollama_generate_endpoint(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with client_factory(timeout=timeout) as client:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def score_document(index: int, document: str) -> dict[str, Any]:
            async with semaphore:
                payload = {
                    "model": model,
                    "prompt": _build_rerank_prompt(query, document),
                    "stream": False,
                    "options": {
                        "temperature": 0,
                        "num_predict": 16,
                    },
                }
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                score = _parse_rerank_score(str(response.json().get("response", "")))
                return {"index": index, "relevance_score": score}

        results = await asyncio.gather(
            *(score_document(index, document) for index, document in enumerate(documents))
        )

    results.sort(key=lambda item: (-item["relevance_score"], item["index"]))
    if top_n is not None:
        return results[:top_n]
    return results
