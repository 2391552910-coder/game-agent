import json
from unittest.mock import patch

import pytest

from src.core.engine import rag_exact_match


def test_extract_query_terms_keeps_domain_terms_and_drops_question_words():
    terms = rag_exact_match.extract_query_terms("原味咖啡多少钱？体育馆正门坐标是多少？")

    assert "原味咖啡" in terms
    assert "体育馆正门" in terms
    assert "多少钱" not in terms
    assert "是多少" not in terms


@pytest.mark.asyncio
async def test_retrieve_exact_rag_context_returns_ranked_matching_chunks():
    class FakeRedis:
        async def scan_iter(self, match: str, count: int):
            assert match == "text_chunks:*"
            assert count == 1000
            yield "text_chunks:1"
            yield "text_chunks:2"

        async def get(self, key: str):
            values = {
                "text_chunks:1": json.dumps(
                    {
                        "content": "【item_price】名称: 原味咖啡\n价格: 15 金币\nID: item_coffee_01",
                        "file_path": "git:game_docs/游戏场景数据.md",
                    },
                    ensure_ascii=False,
                ),
                "text_chunks:2": json.dumps(
                    {
                        "content": "【map_coordinate】名称: 体育馆正门\n坐标: (138, 0, 78)",
                        "file_path": "git:game_docs/游戏场景数据.md",
                    },
                    ensure_ascii=False,
                ),
            }
            return values[key]

        async def aclose(self):
            return None

    with (
        patch("src.config.settings.rag_exact_match_enabled", True, create=True),
        patch("src.config.settings.rag_exact_match_top_k", 3, create=True),
        patch("redis.asyncio.Redis.from_url", return_value=FakeRedis()),
    ):
        context = await rag_exact_match.retrieve_exact_rag_context("原味咖啡多少钱？")

    assert "【精确匹配RAG补充】" in context
    assert "原味咖啡" in context
    assert "15 金币" in context
    assert "体育馆正门" not in context
