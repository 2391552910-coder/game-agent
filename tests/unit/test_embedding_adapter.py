from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.core.engine.embedding import (
    dashscope_multimodal_embed,
    is_dashscope_multimodal_embedding_model,
    resolve_dashscope_multimodal_endpoint,
)


def test_detects_dashscope_multimodal_embedding_models():
    assert is_dashscope_multimodal_embedding_model("tongyi-embedding-vision-flash-2026-03-06")
    assert is_dashscope_multimodal_embedding_model("tongyi-embedding-vision-plus")
    assert is_dashscope_multimodal_embedding_model("qwen3-vl-embedding")
    assert is_dashscope_multimodal_embedding_model("multimodal-embedding-v1")
    assert not is_dashscope_multimodal_embedding_model("text-embedding-v4")


def test_resolves_compatible_mode_url_to_multimodal_endpoint():
    endpoint = resolve_dashscope_multimodal_endpoint(
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    assert endpoint == (
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
        "multimodal-embedding/multimodal-embedding"
    )


@pytest.mark.asyncio
async def test_dashscope_multimodal_embed_posts_text_contents_and_returns_numpy():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "output": {
            "embeddings": [
                {"index": 1, "type": "text", "embedding": [0.3, 0.4]},
                {"index": 0, "type": "text", "embedding": [0.1, 0.2]},
            ]
        }
    }

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=response)

    result = await dashscope_multimodal_embed(
        ["第一段", "第二段"],
        model="tongyi-embedding-vision-flash-2026-03-06",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        client_factory=lambda **_: client,
    )

    client.post.assert_awaited_once()
    _, kwargs = client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["json"] == {
        "model": "tongyi-embedding-vision-flash-2026-03-06",
        "input": {
            "contents": [
                {"text": "第一段"},
                {"text": "第二段"},
            ]
        },
        "parameters": {"output_type": "dense"},
    }
    assert isinstance(result, np.ndarray)
    np.testing.assert_allclose(result, np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32))


@pytest.mark.asyncio
async def test_dashscope_multimodal_embed_omits_dimension_for_flash_model():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "output": {
            "embeddings": [
                {"index": 0, "type": "text", "embedding": [0.1, 0.2]},
            ]
        }
    }

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=response)

    await dashscope_multimodal_embed(
        ["测试"],
        model="tongyi-embedding-vision-flash-2026-03-06",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dim=768,
        client_factory=lambda **_: client,
    )

    _, kwargs = client.post.call_args
    assert "dimension" not in kwargs["json"]["parameters"]
