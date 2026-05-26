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
async def test_initialize_rag_omits_rerank_model_func_when_disabled():
    import src.core.engine.lightrag_engine as engine

    mock_rag = AsyncMock()
    mock_light_rag = MagicMock(return_value=mock_rag)

    with patch.object(engine, "LightRAG", mock_light_rag):
        await engine.initialize_rag()

    kwargs = mock_light_rag.call_args.kwargs
    assert kwargs["rerank_model_func"] is None
