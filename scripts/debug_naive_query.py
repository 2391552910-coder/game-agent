"""调试 naive 模式查询问题"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lightrag import QueryParam

from src.core.engine.lightrag_engine import get_rag, shutdown_rag


def extract_chunk_preview(raw_content: bytes | str, limit: int = 100) -> str:
    """Extract a short preview from a Redis text chunk record."""
    import json

    if isinstance(raw_content, bytes):
        raw_content = raw_content.decode("utf-8")

    chunk_data = json.loads(raw_content)
    chunk_text = chunk_data.get("content") or chunk_data.get("text") or ""
    return chunk_text[:limit]


def build_query_param(mode: str) -> QueryParam:
    """Build a real LightRAG QueryParam for debug queries."""
    return QueryParam(mode=mode, enable_rerank=False)


async def debug_query():
    print("=== Debug Naive Query ===\n")
    
    rag = await get_rag()
    
    # 测试查询
    test_query = "健身房在哪里？坐标是多少？"
    print(f"Query: {test_query}")
    
    # 直接查看向量数据库中的 chunks
    # 先获取所有存储的 chunks
    from redis import asyncio as aioredis
    redis_conn = aioredis.from_url("redis://:myagent@localhost:6379/0")
    keys = await redis_conn.keys("text_chunks:*")
    print(f"\nFound {len(keys)} text chunks in Redis:")
    
    for key in keys[:5]:  # 只显示前5个
        content = await redis_conn.get(key)
        if content:
            try:
                chunk_text = extract_chunk_preview(content)
                print(f"  - {key}: {chunk_text}...")
            except Exception:
                print(f"  - {key}: {content[:100]}...")
    
    # 测试 naive 查询
    print("\n=== Testing Naive Query ===")
    try:
        answer = await rag.aquery(
            test_query,
            param=build_query_param("naive"),
        )
        print(f"Answer: {answer[:200]}...")
    except Exception as e:
        print(f"Error: {e}")
    
    await shutdown_rag()
    await redis_conn.aclose()


if __name__ == "__main__":
    asyncio.run(debug_query())
