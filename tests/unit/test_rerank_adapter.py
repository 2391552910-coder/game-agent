from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.engine.rerank import ollama_rerank


@pytest.mark.asyncio
async def test_ollama_rerank_scores_and_sorts_documents():
    first_response = MagicMock()
    first_response.raise_for_status = MagicMock()
    first_response.json.return_value = {"response": "0.25"}

    second_response = MagicMock()
    second_response.raise_for_status = MagicMock()
    second_response.json.return_value = {"response": "0.90"}

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=[first_response, second_response])

    results = await ollama_rerank(
        query="怎么去健身房？",
        documents=["无关文本", "健身房坐标是 120,0,85"],
        model="dengcao/Qwen3-Reranker-4B:Q4_K_M",
        base_url="http://localhost:11434",
        client_factory=lambda **_: client,
    )

    assert [item["index"] for item in results] == [1, 0]
    assert results[0]["relevance_score"] == pytest.approx(0.9)
    assert results[1]["relevance_score"] == pytest.approx(0.25)
    assert client.post.await_count == 2
    endpoint, kwargs = client.post.call_args_list[0].args[0], client.post.call_args_list[0].kwargs
    assert endpoint == "http://localhost:11434/api/generate"
    assert kwargs["json"]["model"] == "dengcao/Qwen3-Reranker-4B:Q4_K_M"
    assert kwargs["json"]["stream"] is False
    assert "怎么去健身房？" in kwargs["json"]["prompt"]
    assert "无关文本" in kwargs["json"]["prompt"]


@pytest.mark.asyncio
async def test_ollama_rerank_honors_top_n():
    responses = []
    for score in ["0.2", "0.8", "0.6"]:
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"response": score}
        responses.append(response)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=responses)

    results = await ollama_rerank(
        query="query",
        documents=["doc0", "doc1", "doc2"],
        top_n=2,
        model="dengcao/Qwen3-Reranker-4B:Q4_K_M",
        base_url="http://localhost:11434",
        client_factory=lambda **_: client,
    )

    assert [item["index"] for item in results] == [1, 2]
