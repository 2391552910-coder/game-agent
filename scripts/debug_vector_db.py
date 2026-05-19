"""调试向量数据库查询问题"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.engine.lightrag_engine import get_rag, shutdown_rag


async def debug_vector_db():
    print("=== Debug Vector Database ===\n")
    
    rag = await get_rag()
    
    # 检查 rag 对象的属性
    print("=== RAG Object Info ===")
    print(f"RAG type: {type(rag)}")
    
    # 尝试直接访问向量数据库
    if hasattr(rag, '_client'):
        print("\n=== Vector DB Info ===")
        client = rag._client
        print(f"Client type: {type(client)}")
        
        # 检查 chunks collection
        if hasattr(client, 'get_collection'):
            try:
                chunks_collection = client.get_collection('chunks')
                stats = chunks_collection.stats()
                print(f"Chunks collection stats: {stats}")
                
                # 查询所有 chunks
                results = chunks_collection.query(
                    query_texts=["健身房在哪里？坐标是多少？"],
                    n_results=5,
                    output_fields=['text', 'source']
                )
                print(f"\nQuery results:")
                for i, text in enumerate(results.get('texts', [])):
                    print(f"\nResult {i+1}:")
                    for t in text:
                        print(f"  Text: {t[:150]}...")
            except Exception as e:
                print(f"Error querying vector DB: {e}")
    
    # 测试不同模式
    print("\n=== Testing Different Modes ===")
    test_query = "健身房在哪里？坐标是多少？"
    
    from lightrag import QueryParam
    
    for mode in ['naive', 'hybrid']:
        print(f"\nMode: {mode}")
        try:
            answer = await rag.aquery(
                test_query,
                param=QueryParam(mode=mode, enable_rerank=False),
            )
            print(f"Answer length: {len(answer)} chars")
            print(f"Answer preview: {answer[:100]}...")
        except Exception as e:
            print(f"Error: {e}")
    
    await shutdown_rag()


if __name__ == "__main__":
    asyncio.run(debug_vector_db())