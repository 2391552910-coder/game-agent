import json

from lightrag import QueryParam

from scripts.debug_naive_query import build_query_param, extract_chunk_preview


def test_extract_chunk_preview_uses_current_content_field():
    raw = json.dumps({"content": "健身房大门：坐标 (108, 0, 76)。"}).encode("utf-8")

    assert extract_chunk_preview(raw, limit=10) == "健身房大门：坐标 ("


def test_build_query_param_returns_real_lightrag_query_param():
    param = build_query_param("naive")

    assert isinstance(param, QueryParam)
    assert param.mode == "naive"
    assert param.enable_rerank is False
    assert hasattr(param, "model_func")
