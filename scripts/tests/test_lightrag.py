"""LightRAG 集成测试脚本。

测试流程：
1. 初始化 LightRAG 引擎
2. 导入 game_docs/ 星境日常.md
3. 多场景查询测试（基础信息、玩法规则、时间咨询、故障排查）
4. 不同检索模式对比（hybrid / local / global / naive）
5. Rerank 开关对比

用法：
    python -m scripts.tests.test_lightrag
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台编码修复
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from lightrag import QueryParam

from src.core.engine.lightrag_engine import get_rag, shutdown_rag

# 测试配置
GAME_DOCS_DIR = PROJECT_ROOT / "game_docs"

# 测试查询集（覆盖不同检索场景）
TEST_QUERIES = [
    # 场景 1: 地图坐标查询
    {
        "category": "地图坐标",
        "query": "健身房在哪里？坐标是多少？",
        "expected_keywords": ["108", "gym_02", "健身房"],
    },
    {
        "category": "地图坐标",
        "query": "射击场的入口坐标是什么？建筑ID是多少？",
        "expected_keywords": ["125", "shoot_01", "射击场"],
    },
    # 场景 2: 动作技能查询
    {
        "category": "动作技能",
        "query": "play_action 支持哪些动作？",
        "expected_keywords": ["wave", "sit", "dance_normal", "start_shooting"],
    },
    {
        "category": "动作技能",
        "query": "start_shooting 动作在哪里可以使用？",
        "expected_keywords": ["射击场", "坐标范围"],
    },
    # 场景 3: 游戏活动查询
    {
        "category": "游戏活动",
        "query": "有哪些活动适合休闲玩家？",
        "expected_keywords": ["射击打靶", "咖啡打卡", "休闲"],
    },
    {
        "category": "游戏活动",
        "query": "射击打靶活动需要什么条件？消耗多少金币？",
        "expected_keywords": ["LV6", "60", "金币"],
    },
    {
        "category": "游戏活动",
        "query": "健身塑形活动需要什么道具？",
        "expected_keywords": ["月卡", "gym_03"],
    },
    # 场景 4: 经济系统查询
    {
        "category": "经济系统",
        "query": "游戏中有哪些货币类型？",
        "expected_keywords": ["金币", "竞技积分", "钻石", "体力值"],
    },
    {
        "category": "经济系统",
        "query": "健身房月卡多少钱？",
        "expected_keywords": ["280", "item_gym_month_01"],
    },
    {
        "category": "经济系统",
        "query": "钻石怎么兑换金币？",
        "expected_keywords": ["1:100", "兑换"],
    },
]

# 检索模式列表
QUERY_MODES = ["hybrid", "local", "global", "naive"]


# 辅助函数


def load_game_docs() -> tuple[list[str], list[str]]:
    """加载 game_docs/ 目录下所有 .md 文件。

    Returns:
        (texts, file_paths) 元组
    """
    texts = []
    file_paths = []

    for md_file in sorted(GAME_DOCS_DIR.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        texts.append(content)
        file_paths.append(str(md_file.name))
        print(f"  [LOAD] {md_file.name} ({len(content)} chars)")

    return texts, file_paths


def check_keywords(result: str, keywords: list[str]) -> dict:
    """检查结果是否包含预期关键词。

    Returns:
        {"matched": [...], "missing": [...], "score": float}
    """
    matched = [kw for kw in keywords if kw in result]
    missing = [kw for kw in keywords if kw not in result]
    score = len(matched) / len(keywords) if keywords else 0.0
    return {
        "matched": matched,
        "missing": missing,
        "score": score,
    }


def print_separator(title: str, width: 80):
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


# 测试主流程


async def test_insert():
    """测试 1: 文档导入"""
    print_separator("TEST 1: Document Import", 80)

    rag = await get_rag()
    texts, file_paths = load_game_docs()

    if not texts:
        print("  [SKIP] No game docs found")
        return False

    print(f"\n  Importing {len(texts)} documents...")
    start = time.time()
    await rag.ainsert(texts, file_paths=file_paths)
    elapsed = time.time() - start
    print(f"  [OK] Import completed in {elapsed:.1f}s")
    return True


async def test_queries():
    """测试 2: 多场景查询（hybrid 模式 + rerank）"""
    print_separator("TEST 2: Multi-scenario Queries (hybrid + rerank)", 80)

    rag = await get_rag()
    results = []

    for i, test_case in enumerate(TEST_QUERIES, 1):
        category = test_case["category"]
        query = test_case["query"]
        expected = test_case["expected_keywords"]

        print(f"\n  [{i}/{len(TEST_QUERIES)}] [{category}]")
        print(f"  Query: {query}")

        start = time.time()
        try:
            answer = await rag.aquery(
                query,
                param=QueryParam(mode="hybrid", enable_rerank=True),
            )
        except Exception as e:
            answer = None
            print(f"  ERROR: {str(e)[:100]}")
        elapsed = time.time() - start

        if answer is None:
            keyword_check = {"matched": [], "missing": expected, "score": 0.0}
        else:
            keyword_check = check_keywords(answer, expected)

        print(f"  Time: {elapsed:.1f}s")
        print(f"  Keywords: {keyword_check['score']:.0%} ({len(keyword_check['matched'])}/{len(expected)})")
        if keyword_check["matched"]:
            print(f"    MATCH: {', '.join(keyword_check['matched'])}")
        if keyword_check["missing"]:
            print(f"    MISS:  {', '.join(keyword_check['missing'])}")
        if answer is not None:
            print(f"  Answer: {answer[:120]}...")
        else:
            print(f"  Answer: None (query failed)")

        results.append(
            {
                "category": category,
                "query": query,
                "elapsed": elapsed,
                "keyword_score": keyword_check["score"],
                "answer_length": len(answer) if answer else 0,
            }
        )

    # 汇总统计
    avg_score = sum(r["keyword_score"] for r in results) / len(results)
    avg_time = sum(r["elapsed"] for r in results) / len(results)
    print_separator("Query Test Summary", 80)
    print(f"  Avg keyword match: {avg_score:.0%}")
    print(f"  Avg response time: {avg_time:.1f}s")
    print(f"  Total queries: {len(results)}")

    return results


async def test_query_modes():
    """测试 3: 不同检索模式对比"""
    print_separator("TEST 3: Retrieval Mode Comparison", 80)

    rag = await get_rag()
    test_query = "工作区周末开放吗？几点到几点？"

    print(f"  Query: {test_query}\n")

    mode_results = {}
    for mode in QUERY_MODES:
        print(f"  Mode: {mode:8s}", end="  ")
        start = time.time()
        try:
            answer = await rag.aquery(
                test_query,
                param=QueryParam(mode=mode, enable_rerank=True),
            )
            elapsed = time.time() - start
            print(f"OK {elapsed:.1f}s  ({len(answer)} chars)")
            print(f"    Preview: {answer[:100]}...")
            mode_results[mode] = {
                "elapsed": elapsed,
                "length": len(answer),
                "success": True,
            }
        except Exception as e:
            print(f"ERROR: {e}")
            mode_results[mode] = {
                "elapsed": 0,
                "length": 0,
                "success": False,
                "error": str(e),
            }

    return mode_results


async def test_rerank_toggle():
    """测试 4: Rerank 开关对比"""
    print_separator("TEST 4: Rerank Toggle", 80)

    rag = await get_rag()
    test_query = "周末集市什么时候开放？"

    for enable_rerank in [True, False]:
        tag = "rerank ON" if enable_rerank else "rerank OFF"
        print(f"\n  {tag}")
        start = time.time()
        answer = await rag.aquery(
            test_query,
            param=QueryParam(mode="naive", enable_rerank=enable_rerank),
        )
        elapsed = time.time() - start
        print(f"  Time: {elapsed:.1f}s")
        print(f"  Answer: {answer[:150]}...")


async def test_game_data_query_modes():
    """测试 5: 游戏场景数据的四种检索模式对比"""
    print_separator("TEST 5: Game Data Retrieval Mode Comparison", 80)

    rag = await get_rag()
    test_query = "健身房在哪里？坐标是多少？"

    print(f"  Query: {test_query}\n")

    # 先检查向量数据库状态
    print("  === Vector DB Status ===")
    try:
        # 尝试直接检查 Milvus 中的 chunks
        if hasattr(rag, '_db') and hasattr(rag._db, '_vector_db'):
            vector_db = rag._db._vector_db
            if hasattr(vector_db, 'client'):
                collection = vector_db.client.get_collection('chunks')
                stats = collection.stats()
                print(f"  Milvus chunks collection stats: {stats}")
    except Exception as e:
        print(f"  Could not check Milvus stats: {e}")

    mode_results = {}
    for mode in QUERY_MODES:
        print(f"\n  Mode: {mode:8s}", end="  ")
        start = time.time()
        try:
            answer = await rag.aquery(
                test_query,
                param=QueryParam(mode=mode, enable_rerank=True),
            )
            elapsed = time.time() - start
            print(f"OK {elapsed:.1f}s  ({len(answer)} chars)")
            print(f"    Preview: {answer[:100]}...")
            
            keyword_check = check_keywords(answer, ["108", "gym_02", "健身房"])
            print(f"    Keywords: {keyword_check['score']:.0%} matched")
            
            mode_results[mode] = {
                "elapsed": elapsed,
                "length": len(answer),
                "success": True,
                "keyword_score": keyword_check["score"],
            }
        except Exception as e:
            print(f"ERROR: {e}")
            mode_results[mode] = {
                "elapsed": 0,
                "length": 0,
                "success": False,
                "error": str(e),
                "keyword_score": 0,
            }

    # 汇总统计
    print_separator("Game Data Mode Comparison Summary", 80)
    print(f"  Query: {test_query}")
    print(f"  {'-' * 70}")
    print(f"  {'Mode':<10} {'Time':<8} {'Length':<10} {'Keywords':<10}")
    print(f"  {'-' * 70}")
    for mode, result in mode_results.items():
        if result["success"]:
            print(f"  {mode:<10} {result['elapsed']:.2f}s    {result['length']:<10} {result['keyword_score']:.0%}")
        else:
            print(f"  {mode:<10} ERROR")

    return mode_results


async def test_context_only():
    """测试 6: 仅上下文模式（供 LangGraph 用）"""
    print_separator("TEST 6: Context Only Mode (for LangGraph)", 80)

    rag = await get_rag()
    test_query = "星境城有哪些功能区域？"

    print(f"  Query: {test_query}\n")
    context = await rag.aquery(
        test_query,
        param=QueryParam(mode="hybrid", only_need_context=True),
    )
    print(f"  Retrieved context ({len(context)} chars):")
    print(f"  {'-' * 60}")
    print(f"  {context[:500]}...")
    print(f"  {'-' * 60}")


# 主入口


async def main():
    print("LightRAG Integration Test")
    print(f"  Game docs dir: {GAME_DOCS_DIR}")
    print(f"  Working dir: {os.environ.get('RAG_WORKING_DIR', 'N/A')}")
    print()

    try:
        # 测试 1: 文档导入
        insert_ok = await test_insert()
        if not insert_ok:
            print("\n[WARN] Import failed, skipping remaining tests")
            return

        # 短暂等待索引构建
        print("\n  Waiting for index build (3s)...")
        await asyncio.sleep(3)

        # 测试 2: 多场景查询
        await test_queries()

        # 测试 3: 检索模式对比
        await test_query_modes()

        # 测试 4: Rerank 开关
        await test_rerank_toggle()

        # 测试 5: 游戏场景数据的四种检索模式对比
        await test_game_data_query_modes()

        # 测试 6: 仅上下文模式
        await test_context_only()

        print_separator("ALL TESTS COMPLETED", 80)
        print("  [OK] All tests passed")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback

        traceback.print_exc()

    finally:
        await shutdown_rag()
        print("\n  LightRAG shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
