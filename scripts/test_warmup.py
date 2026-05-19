"""测试连接池预热功能"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lightrag import QueryParam
from src.core.engine.lightrag_engine import get_rag, shutdown_rag, warmup_connections


async def test_with_warmup():
    """测试带预热的查询性能"""
    print("=== Test with Connection Warmup ===\n")
    
    # 步骤1: 预热连接池
    print("Step 1: Warming up connections...")
    warmup_start = time.time()
    await warmup_connections()
    warmup_time = time.time() - warmup_start
    print(f"\nWarmup completed in {warmup_time:.2f}s\n")
    
    # 步骤2: 清空缓存
    print("Step 2: Clearing cache...")
    from redis import asyncio as aioredis
    redis_conn = aioredis.from_url("redis://:myagent@localhost:6379/0")
    await redis_conn.flushdb()
    await redis_conn.close()
    print("Cache cleared\n")
    
    # 步骤3: 测试首次查询
    print("Step 3: Testing first query (after warmup)...")
    rag = await get_rag()
    
    test_query = "健身房在哪里？坐标是多少？"
    print(f"Query: {test_query}\n")
    
    start = time.time()
    answer = await rag.aquery(
        test_query,
        param=QueryParam(mode="hybrid", enable_rerank=True),
    )
    elapsed = time.time() - start
    
    print(f"First query time: {elapsed:.2f}s")
    print(f"Answer length: {len(answer)} chars")
    print(f"Answer preview: {answer[:100]}...\n")
    
    await shutdown_rag()
    
    return elapsed


async def test_without_warmup():
    """测试不带预热的查询性能（对比）"""
    print("\n=== Test without Connection Warmup ===\n")
    
    # 清空缓存
    print("Step 1: Clearing cache...")
    from redis import asyncio as aioredis
    redis_conn = aioredis.from_url("redis://:myagent@localhost:6379/0")
    await redis_conn.flushdb()
    await redis_conn.close()
    print("Cache cleared\n")
    
    # 直接测试首次查询（不预热）
    print("Step 2: Testing first query (without warmup)...")
    rag = await get_rag()
    
    test_query = "健身房在哪里？坐标是多少？"
    print(f"Query: {test_query}\n")
    
    start = time.time()
    answer = await rag.aquery(
        test_query,
        param=QueryParam(mode="hybrid", enable_rerank=True),
    )
    elapsed = time.time() - start
    
    print(f"First query time: {elapsed:.2f}s")
    print(f"Answer length: {len(answer)} chars")
    print(f"Answer preview: {answer[:100]}...\n")
    
    await shutdown_rag()
    
    return elapsed


async def main():
    # 测试带预热的情况
    time_with_warmup = await test_with_warmup()
    
    # 测试不带预热的情况
    time_without_warmup = await test_without_warmup()
    
    # 对比结果
    print("=== Performance Comparison ===")
    print(f"With warmup:    {time_with_warmup:.2f}s")
    print(f"Without warmup: {time_without_warmup:.2f}s")
    
    improvement = ((time_without_warmup - time_with_warmup) / time_without_warmup) * 100
    print(f"\nImprovement: {improvement:.1f}% faster")


if __name__ == "__main__":
    asyncio.run(main())