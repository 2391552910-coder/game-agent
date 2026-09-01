"""Embedding provider adapters used by LightRAG."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

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

_OLLAMA_HOSTS = {"localhost:11434", "127.0.0.1:11434", "[::1]:11434"}


def is_dashscope_multimodal_embedding_model(model: str) -> bool:
    """Return whether a model requires DashScope native multimodal embedding API."""
    normalized_model = model.lower()
    return any(marker in normalized_model for marker in _DASHSCOPE_MULTIMODAL_MODEL_MARKERS)


def is_ollama_embedding_base_url(base_url: str) -> bool:
    """Return whether the configured embedding base URL points at a local Ollama server."""
    parsed_url = urlparse(base_url)
    return parsed_url.netloc.lower() in _OLLAMA_HOSTS


def resolve_ollama_embed_endpoint(base_url: str) -> str:
    """Resolve an Ollama base URL or OpenAI-compatible URL to the native embed endpoint."""
    normalized_base_url = base_url.rstrip("/")
    if normalized_base_url.endswith("/api/embed"):
        return normalized_base_url
    if normalized_base_url.endswith("/v1"):
        normalized_base_url = normalized_base_url[: -len("/v1")]
    if normalized_base_url.endswith("/api"):
        return f"{normalized_base_url}/embed"
    return f"{normalized_base_url}/api/embed"


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


def _parse_ollama_embeddings(payload: dict[str, Any], expected_count: int) -> np.ndarray:
    embeddings = payload.get("embeddings")
    if embeddings is None and expected_count == 1 and isinstance(payload.get("embedding"), list):
        embeddings = [payload["embedding"]]

    if not isinstance(embeddings, list):
        raise ValueError("Ollama embedding response missing embeddings")
    if len(embeddings) != expected_count:
        raise ValueError(f"Ollama embedding returned {len(embeddings)} vectors, expected {expected_count}")

    return np.array(embeddings, dtype=np.float32)


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


async def ollama_embed(
    texts: list[str],
    *,
    model: str,
    base_url: str,
    embedding_dim: int | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> np.ndarray:
    """Embed text with Ollama native /api/embed."""
    if not texts:
        return np.empty((0, embedding_dim or 0), dtype=np.float32)

    payload: dict[str, Any] = {
        "model": model,
        "input": texts,
    }
    if embedding_dim is not None:
        payload["dimensions"] = embedding_dim

    async with client_factory(timeout=60) as client:
        response = await client.post(resolve_ollama_embed_endpoint(base_url), json=payload)
        response.raise_for_status()
        return _parse_ollama_embeddings(response.json(), expected_count=len(texts))


async def embed_texts(
    texts: list[str],
    *,
    model: str,
    api_key: str,
    base_url: str,
    embedding_dim: int | None = None,
) -> np.ndarray:
    """Embed text using the configured provider-specific API."""
    if is_ollama_embedding_base_url(base_url):
        return await ollama_embed(
            texts,
            model=model,
            base_url=base_url,
            embedding_dim=embedding_dim,
        )

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
        embedding_dim=embedding_dim,
    )
