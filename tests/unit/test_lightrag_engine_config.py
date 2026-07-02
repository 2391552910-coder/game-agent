from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_initialize_rag_passes_configured_llm_concurrency():
    import src.core.engine.lightrag_engine as engine

    mock_rag = AsyncMock()
    mock_light_rag = MagicMock(return_value=mock_rag)

    with patch.object(engine, "LightRAG", mock_light_rag):
        await engine.initialize_rag()

    kwargs = mock_light_rag.call_args.kwargs
    assert kwargs["llm_model_max_async"] == engine.settings.lightrag_llm_max_async


@pytest.mark.asyncio
async def test_initialize_rag_passes_configured_chunk_sizes():
    import src.core.engine.lightrag_engine as engine

    mock_rag = AsyncMock()
    mock_light_rag = MagicMock(return_value=mock_rag)

    with patch.object(engine, "LightRAG", mock_light_rag):
        await engine.initialize_rag()

    kwargs = mock_light_rag.call_args.kwargs
    assert kwargs["chunk_token_size"] == engine.settings.lightrag_chunk_token_size
    assert (
        kwargs["chunk_overlap_token_size"]
        == engine.settings.lightrag_chunk_overlap_token_size
    )


@pytest.mark.asyncio
async def test_initialize_rag_passes_configured_vector_cosine_threshold():
    import src.core.engine.lightrag_engine as engine

    mock_rag = AsyncMock()
    mock_light_rag = MagicMock(return_value=mock_rag)

    with (
        patch.object(engine.settings, "lightrag_vector_cosine_threshold", 0.42, create=True),
        patch.object(engine, "LightRAG", mock_light_rag),
    ):
        await engine.initialize_rag()

    kwargs = mock_light_rag.call_args.kwargs
    vector_kwargs = kwargs["vector_db_storage_cls_kwargs"]
    assert vector_kwargs["cosine_better_than_threshold"] == 0.42


@pytest.mark.asyncio
async def test_initialize_rag_omits_rerank_model_func_when_disabled():
    import src.core.engine.lightrag_engine as engine

    mock_rag = AsyncMock()
    mock_light_rag = MagicMock(return_value=mock_rag)

    with (
        patch.object(engine.settings, "rerank_enabled", False),
        patch.object(engine, "LightRAG", mock_light_rag),
    ):
        await engine.initialize_rag()

    kwargs = mock_light_rag.call_args.kwargs
    assert kwargs["rerank_model_func"] is None


@pytest.mark.asyncio
async def test_initialize_rag_uses_ollama_rerank_model_func_when_enabled():
    import src.core.engine.lightrag_engine as engine

    mock_rag = AsyncMock()
    mock_light_rag = MagicMock(return_value=mock_rag)

    with (
        patch.object(engine.settings, "rerank_enabled", True),
        patch.object(engine.settings, "rerank_model", "dengcao/Qwen3-Reranker-4B:Q4_K_M"),
        patch.object(engine.settings, "rerank_base_url", "http://localhost:11434"),
        patch.object(engine.settings, "rerank_max_concurrency", 1, create=True),
        patch.object(engine, "LightRAG", mock_light_rag),
    ):
        await engine.initialize_rag()

    kwargs = mock_light_rag.call_args.kwargs
    rerank_model_func = kwargs["rerank_model_func"]
    assert rerank_model_func is not None
    assert rerank_model_func.keywords["model"] == "dengcao/Qwen3-Reranker-4B:Q4_K_M"
    assert rerank_model_func.keywords["base_url"] == "http://localhost:11434"
    assert rerank_model_func.keywords["max_concurrency"] == 1
