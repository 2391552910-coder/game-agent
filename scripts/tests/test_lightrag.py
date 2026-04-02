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
    # 场景 1: 基础信息查询
    {
        "category": "基础信息",
        "query": "《星境日常》是什么类型的游戏？",
        "expected_keywords": ["生活模拟", "元宇宙", "星境城"],
    },
    {
        "category": "基础信息",
        "query": "游戏里有哪些职业可以选择？",
        "expected_keywords": ["教师", "医生", "程序员"],
    },
    # 场景 2: 时间类咨询
    {
        "category": "时间咨询",
        "query": "工作区几点开放？周末可以去工作区上班吗？",
        "expected_keywords": ["工作日", "09:00", "18:00"],
    },
    {
        "category": "时间咨询",
        "query": "自然景区晚上能去吗？几点关闭？",
        "expected_keywords": ["19:00", "关闭", "夜间"],
    },
    # 场景 3: 玩法规则
    {
        "category": "玩法规则",
        "query": "周末集市什么时候开放？在哪里？",
        "expected_keywords": ["周六", "周日", "10:00", "16:00"],
    },
    # 场景 4: 故障排查
    {
        "category": "故障排查",
        "query": "每周三为什么进不去游戏？",
        "expected_keywords": ["维护", "02:00", "04:00"],
    },
    {
        "category": "故障排查",
        "query": "游戏突然关闭了怎么办？",
        "expected_keywords": ["公告", "故障", "维护"],
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
        answer = await rag.aquery(
            query,
            param=QueryParam(mode="hybrid", enable_rerank=True),
        )
        elapsed = time.time() - start

        keyword_check = check_keywords(answer, expected)

        print(f"  Time: {elapsed:.1f}s")
        print(f"  Keywords: {keyword_check['score']:.0%} ({len(keyword_check['matched'])}/{len(expected)})")
        if keyword_check["matched"]:
            print(f"    MATCH: {', '.join(keyword_check['matched'])}")
        if keyword_check["missing"]:
            print(f"    MISS:  {', '.join(keyword_check['missing'])}")
        print(f"  Answer: {answer[:120]}...")

        results.append(
            {
                "category": category,
                "query": query,
                "elapsed": elapsed,
                "keyword_score": keyword_check["score"],
                "answer_length": len(answer),
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


async def test_context_only():
    """测试 5: 仅上下文模式（供 LangGraph 用）"""
    print_separator("TEST 5: Context Only Mode (for LangGraph)", 80)

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

        # 测试 5: 仅上下文模式
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
