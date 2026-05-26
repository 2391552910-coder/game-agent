"""Embedding provider adapters used by LightRAG."""

from collections.abc import Callable
from typing import Any

import httpx
import numpy as np
from lightrag.llm.openai import openai_embed

DASHSCOPE_MULTIMODAL_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)

_DASHSCOPE_MULTIMODAL_MODEL_MARKERS = (
    "tongyi-embedding-vision",
    "qwen-vl-embedding",
    "qwen3-vl-embedding",
    "multimodal-embedding",
)


def is_dashscope_multimodal_embedding_model(model: str) -> bool:
    """Return whether a model requires DashScope native multimodal embedding API."""
    normalized_model = model.lower()
    return any(marker in normalized_model for marker in _DASHSCOPE_MULTIMODAL_MODEL_MARKERS)


def resolve_dashscope_multimodal_endpoint(base_url: str) -> str:
    """Resolve a configured embedding base URL to the DashScope multimodal endpoint."""
    normalized_base_url = base_url.rstrip("/")
    if normalized_base_url.endswith("/compatible-mode/v1"):
        return DASHSCOPE_MULTIMODAL_ENDPOINT
    if normalized_base_url.endswith("/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"):
        return normalized_base_url
    return f"{normalized_base_url}/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"


def _parse_dashscope_embeddings(payload: dict[str, Any], expected_count: int) -> np.ndarray:
    embeddings = payload.get("output", {}).get("embeddings")
    if not isinstance(embeddings, list):
        raise ValueError("DashScope multimodal embedding response missing output.embeddings")

    vectors_by_index: dict[int, list[float]] = {}
    for item in embeddings:
        if not isinstance(item, dict):
            continue
        vector = item.get("embedding")
        index = item.get("index")
        if not isinstance(vector, list) or not isinstance(index, int):
            continue
        vectors_by_index[index] = vector

    vectors = [vectors_by_index[i] for i in range(expected_count) if i in vectors_by_index]
    if len(vectors) != expected_count:
        raise ValueError(
            f"DashScope multimodal embedding returned {len(vectors)} vectors, expected {expected_count}"
        )

    return np.array(vectors, dtype=np.float32)


async def dashscope_multimodal_embed(
    texts: list[str],
    *,
    model: str,
    api_key: str,
    base_url: str,
    embedding_dim: int | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> np.ndarray:
    """Embed text with DashScope native multimodal embedding API."""
    endpoint = resolve_dashscope_multimodal_endpoint(base_url)
    payload: dict[str, Any] = {
        "model": model,
        "input": {"contents": [{"text": text} for text in texts]},
        "parameters": {"output_type": "dense"},
    }

    normalized_model = model.lower()
    if embedding_dim is not None and "flash" not in normalized_model:
        payload["parameters"]["dimension"] = embedding_dim

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with client_factory(timeout=60) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        return _parse_dashscope_embeddings(response.json(), expected_count=len(texts))


async def embed_texts(
    texts: list[str],
    *,
    model: str,
    api_key: str,
    base_url: str,
    embedding_dim: int | None = None,
) -> np.ndarray:
    """Embed text using the configured provider-specific API."""
    if is_dashscope_multimodal_embedding_model(model):
        return await dashscope_multimodal_embed(
            texts,
            model=model,
            api_key=api_key,
            base_url=base_url,
            embedding_dim=embedding_dim,
        )

    return await openai_embed.func(
        texts,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )
