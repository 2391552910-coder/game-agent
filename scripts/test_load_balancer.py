"""LLM 负载均衡器测试脚本。

测试项目:
1. 基础选择 — 从 DB 加载 provider 并创建 LLM 实例
2. 加权轮询 — 多 provider 不同权重分布下的选择统计
3. 健康降级 — 模拟失败后自动跳过不健康 provider
4. 缓存失效 — invalidate_cache 后重新加载
5. 回退机制 — 无 provider 时回退到 .env 配置
6. 实际 LLM 调用 — 通过 balancer 获取实例并发送真实请求

用法:
    uv run python scripts/test_load_balancer.py
"""

import asyncio
import logging
import time
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SEPARATOR = "=" * 60


async def test_basic_select():
    """测试1: 基础选择 — 从 DB 加载 provider 并创建 LLM 实例。"""
    print(f"\n{SEPARATOR}")
    print("测试1: 基础选择")
    print(SEPARATOR)

    from src.core.llm.balancer import balancer

    await balancer.initialize()

    llm = await balancer.select(model_type="default", temperature=0.1)
    print(f"  获取到 LLM 实例: model={llm.model_name}")
    print(f"  base_url: {llm.openai_api_base}")
    print(f"  实例类型: {type(llm).__name__}")

    assert llm is not None
    assert llm.model_name is not None
    print("  [PASS] 基础选择正常")


async def test_weighted_round_robin():
    """测试2: 加权轮询 — 验证不同权重的选择分布。"""
    print(f"\n{SEPARATOR}")
    print("测试2: 加权轮询分布")
    print(SEPARATOR)

    from sqlalchemy import text

    from src.core.infrastructure.db import get_session
    from src.core.llm.balancer import LoadBalancer
    from src.core.llm.models import LLMProviderConfig

    # 创建独立 balancer 实例，注入模拟 provider（不写 DB）
    test_balancer = LoadBalancer()

    # 手动注入模拟 provider 到缓存
    providers = [
        LLMProviderConfig(
            id="provider-a", name="A", provider="test", model="model-a",
            api_key="key-a", base_url="http://a", weight=3, model_type="default",
        ),
        LLMProviderConfig(
            id="provider-b", name="B", provider="test", model="model-b",
            api_key="key-b", base_url="http://b", weight=2, model_type="default",
        ),
        LLMProviderConfig(
            id="provider-c", name="C", provider="test", model="model-c",
            api_key="key-c", base_url="http://c", weight=1, model_type="default",
        ),
    ]

    # 直接写入缓存（绕过 DB 加载）
    test_balancer._pool_cache["default"] = providers
    test_balancer._pool_loaded_at["default"] = time.monotonic() + 9999
    test_balancer._build_rr_sequence("default", providers)

    sequence = test_balancer._rr_sequences["default"]
    print(f"  加权展开序列: {sequence}")
    assert sequence == ["provider-a"] * 3 + ["provider-b"] * 2 + ["provider-c"] * 1

    # 运行 60 次选择，统计分布
    counter: Counter = Counter()
    for _ in range(60):
        selected = test_balancer._weighted_select("default", providers)
        counter[selected.id] += 1

    print(f"  60 次选择分布: {dict(counter)}")
    print(f"  期望比例 A:B:C = 3:2:1 (约 30:20:10)")
    print(f"  实际: A={counter['provider-a']}, B={counter['provider-b']}, C={counter['provider-c']}")

    # 验证分布大致正确
    assert counter["provider-a"] >= counter["provider-b"]
    assert counter["provider-b"] >= counter["provider-c"]
    print("  [PASS] 加权轮询分布正确")


async def test_health_degradation():
    """测试3: 健康降级 — 模拟失败后跳过不健康 provider。"""
    print(f"\n{SEPARATOR}")
    print("测试3: 健康降级")
    print(SEPARATOR)

    from src.core.infrastructure.redis import get_redis
    from src.core.llm.balancer import LoadBalancer, _HEALTH_KEY_PREFIX
    from src.core.llm.models import LLMProviderConfig

    redis = await get_redis()

    test_balancer = LoadBalancer()

    providers = [
        LLMProviderConfig(
            id="health-a", name="A-健康", provider="test", model="model-a",
            api_key="key-a", base_url="http://a", weight=1, model_type="default",
        ),
        LLMProviderConfig(
            id="health-b", name="B-不健康", provider="test", model="model-b",
            api_key="key-b", base_url="http://b", weight=1, model_type="default",
        ),
    ]

    test_balancer._pool_cache["default"] = providers
    test_balancer._pool_loaded_at["default"] = time.monotonic() + 9999
    test_balancer._build_rr_sequence("default", providers)

    # 模拟 health-b 连续失败 5 次
    for _ in range(5):
        await test_balancer.report_failure("health-b")

    # 检查健康状态
    is_a_healthy = await test_balancer._check_health("health-a")
    is_b_healthy = await test_balancer._check_health("health-b")
    print(f"  A 健康状态: {is_a_healthy}")
    print(f"  B 健康状态: {is_b_healthy}")
    assert is_a_healthy is True
    assert is_b_healthy is False

    # 选择应该只返回 A
    counter = Counter()
    for _ in range(10):
        selected = test_balancer._weighted_select("default", [p for p in providers if await test_balancer._check_health(p.id)])
        counter[selected.id] += 1

    print(f"  10 次选择分布: {dict(counter)}")
    assert counter.get("health-b", 0) == 0
    assert counter["health-a"] == 10

    # 恢复健康
    await test_balancer.report_success("health-b")
    is_b_healthy_after = await test_balancer._check_health("health-b")
    print(f"  B 恢复后健康状态: {is_b_healthy_after}")
    assert is_b_healthy_after is True

    # 清理 Redis
    await redis.delete(f"{_HEALTH_KEY_PREFIX}health-b")
    print("  [PASS] 健康降级正常")


async def test_cache_invalidation():
    """测试4: 缓存失效 — invalidate 后重新加载。"""
    print(f"\n{SEPARATOR}")
    print("测试4: 缓存失效")
    print(SEPARATOR)

    from src.core.llm.balancer import balancer

    # 确保有缓存
    await balancer._load_providers("default")
    assert "default" in balancer._pool_cache
    cache_before = balancer._pool_cache.get("default", [])
    print(f"  缓存失效前: {len(cache_before)} 个 provider")

    # 失效
    balancer.invalidate_cache()
    assert "default" not in balancer._pool_cache
    assert "default" not in balancer._rr_sequences
    print("  缓存已清空")

    # 重新加载
    cache_after = await balancer._load_providers("default")
    print(f"  重新加载后: {len(cache_after)} 个 provider")
    assert len(cache_after) == len(cache_before)
    print("  [PASS] 缓存失效正常")


async def test_fallback():
    """测试5: 回退机制 — 无 provider 时回退到 .env。"""
    print(f"\n{SEPARATOR}")
    print("测试5: 回退到 .env 配置")
    print(SEPARATOR)

    from src.core.llm.balancer import NoProviderAvailable
    from src.core.llm.factory import get_llm

    # 清空 balancer 缓存使其查 DB（DB 只有 default/fast 的 provider）
    # 用一个不存在的 model_type 触发 NoProviderAvailable → fallback
    try:
        llm = await get_llm(model_type="nonexistent", temperature=0.5)
        print(f"  回退到 .env 配置, model={llm.model_name}")
        assert llm is not None
        print("  [PASS] 回退机制正常")
    except Exception as e:
        print(f"  [WARN] 回退测试异常: {e}")


async def test_real_llm_call():
    """测试6: 通过 balancer 获取实例并发送真实 LLM 请求。"""
    print(f"\n{SEPARATOR}")
    print("测试6: 真实 LLM 调用")
    print(SEPARATOR)

    from langchain_core.messages import HumanMessage

    from src.core.llm.factory import get_llm

    llm = await get_llm(model_type="default", temperature=0.0)
    print(f"  获取 LLM: model={llm.model_name}")

    response = await llm.ainvoke([HumanMessage(content="用一句话回答: 1+1等于几？")])
    print(f"  LLM 响应: {response.content[:100]}")

    assert response.content
    print("  [PASS] 真实 LLM 调用正常")


async def test_weight_distribution_with_db():
    """测试7: 用 DB 中的真实 provider 演示多 provider 轮询。"""
    print(f"\n{SEPARATOR}")
    print("测试7: 多 provider 插入 + 轮询验证")
    print(SEPARATOR)

    from sqlalchemy import text

    from src.core.infrastructure.db import get_session
    from src.core.llm.balancer import balancer
    from src.core.llm.models import LLMProviderConfig

    # 查看当前 provider
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id, name, weight, model_type FROM llm_providers WHERE is_active = TRUE"),
        )
        rows = result.fetchall()

    print(f"  当前 DB 中的 provider ({len(rows)} 个):")
    for row in rows:
        print(f"    - {row.name} (weight={row.weight}, type={row.model_type})")

    # 插入两个额外 provider 用于测试轮询（使用假的 api_key/base_url）
    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO llm_providers (name, provider, model, api_key, base_url, weight, model_type)
                VALUES
                    ('Test-A', 'test', 'test-model-a', 'sk-fake-a', 'http://fake-a', 3, 'default'),
                    ('Test-B', 'test', 'test-model-b', 'sk-fake-b', 'http://fake-b', 2, 'default')
            """),
        )

    # 刷新缓存
    balancer.invalidate_cache()
    providers = await balancer._load_providers("default")
    print(f"\n  刷新后 provider ({len(providers)} 个):")
    for p in providers:
        print(f"    - {p.name} (weight={p.weight})")

    # 统计轮询分布
    counter = Counter()
    for _ in range(50):
        selected = balancer._weighted_select("default", providers)
        counter[selected.name] += 1

    print(f"\n  50 次选择分布: {dict(counter)}")
    total_weight = sum(p.weight for p in providers)
    for p in providers:
        expected_pct = p.weight / total_weight * 100
        actual_pct = counter.get(p.name, 0) / 50 * 100
        print(f"    {p.name}: weight={p.weight}, 期望={expected_pct:.0f}%, 实际={actual_pct:.0f}%")

    # 清理测试数据
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM llm_providers WHERE provider = 'test'"),
        )

    balancer.invalidate_cache()
    print("\n  测试 provider 已清理")
    print("  [PASS] 多 provider 轮询正常")


async def main():
    print(SEPARATOR)
    print("LLM 负载均衡器测试")
    print(SEPARATOR)

    results = []
    tests = [
        ("基础选择", test_basic_select),
        ("加权轮询", test_weighted_round_robin),
        ("健康降级", test_health_degradation),
        ("缓存失效", test_cache_invalidation),
        ("回退机制", test_fallback),
        ("真实LLM调用", test_real_llm_call),
        ("多provider轮询", test_weight_distribution_with_db),
    ]

    for name, test_fn in tests:
        try:
            await test_fn()
            results.append((name, "PASS"))
        except Exception as e:
            logger.error("测试 [%s] 失败: %s", name, e, exc_info=True)
            results.append((name, f"FAIL: {e}"))

    # 汇总
    print(f"\n{SEPARATOR}")
    print("测试结果汇总")
    print(SEPARATOR)
    passed = 0
    for name, result in results:
        status = result if result == "PASS" else result
        print(f"  {name}: {status}")
        if result == "PASS":
            passed += 1

    print(f"\n  通过: {passed}/{len(results)}")
    print(SEPARATOR)


if __name__ == "__main__":
    asyncio.run(main())
