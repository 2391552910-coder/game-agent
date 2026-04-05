"""Agent 全流程测试脚本。

直接调用 LangGraph 图，验证完整的分析流水线：
fetch_snapshot → retrieve_rag_context → gather_context
→ behavior_analysis → action_reasoning → merge_output

用法:
    uv run python scripts/test_agent_flow.py
"""

import asyncio
import json
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 测试用玩家快照（星境日常 - 生活模拟游戏角色）
TEST_SNAPSHOT = {
    "user_id": "player_xj_001",
    "player_name": "李逍遥",
    "level": 28,
    "profession": "程序员",
    "current_area": "商业区",
    "guild": None,
    "stats": {
        "play_hours": 180,
        "work_days_attended": 22,
        "shopping_count": 45,
        "leisure_hours": 60,
        "learning_courses": 8,
    },
    "recent_activities": ["购物", "健身", "学习编程课程"],
    "online_time": "20:00-23:00",
}

# 使用 seed_data 中的 alpha 租户
# 需要从数据库查询实际的 tenant_id
TENANT_USER_ID = "game_server_alpha"


async def main():
    # ── 初始化基础设施 ──
    from src.core.infrastructure.db import get_session
    from sqlalchemy import text

    # 查询 alpha 租户 ID
    async with get_session() as session:
        row = await session.execute(
            text("SELECT id FROM tenants WHERE user_id = :uid"),
            {"uid": TENANT_USER_ID},
        )
        result = row.first()

    if not result:
        print("错误: 未找到 alpha 租户，请先运行 seed_data.py")
        sys.exit(1)

    tenant_id = str(result.id)
    print(f"租户: {TENANT_USER_ID} ({tenant_id})")
    print(f"实体: {TEST_SNAPSHOT['user_id']} ({TEST_SNAPSHOT['player_name']})")
    print("=" * 60)

    # ── 构建图（无 checkpointer） ──
    from src.core.agents.orchestrator import build_orchestrator

    graph = build_orchestrator().compile()
    print("[图构建完成] 6 个节点已注册\n")

    # ── 执行 ──
    start_time = time.time()

    initial_state = {
        "user_id": TEST_SNAPSHOT["user_id"],
        "tenant_id": tenant_id,
        "snapshot": TEST_SNAPSHOT,
        "rag_context": "",
        "enriched_context": "",
        "behavior_report": "",
        "reasoned_actions": [],
        "final_output": {},
        "errors": [],
    }

    try:
        result = await graph.ainvoke(initial_state)
    except Exception as e:
        logger.error("Agent 执行失败: %s", e, exc_info=True)
        sys.exit(1)

    elapsed = time.time() - start_time

    # ── 输出结果 ──
    print("\n" + "=" * 60)
    print("AGENT 领域无关流程测试结果")
    print("=" * 60)

    errors = result.get("errors", [])
    if errors:
        print(f"\n[错误] ({len(errors)} 个):")
        for err in errors:
            print(f"  - {err}")

    rag_ctx = result.get("rag_context", "")
    print(f"\n[RAG 上下文] 长度: {len(rag_ctx)} 字符")
    if rag_ctx:
        print(f"  预览: {rag_ctx[:200]}...")

    enriched = result.get("enriched_context", "")
    print(f"\n[工具收集上下文] 长度: {len(enriched)} 字符")
    if enriched:
        print(f"  内容: {enriched[:500]}")

    final = result.get("final_output", {})
    if final:
        output_json = json.dumps(final, ensure_ascii=False, indent=2)
        print(f"\n[最终输出] ({len(output_json)} 字符):")
        print(output_json[:3000])
        if len(output_json) > 3000:
            print("  ... (截断)")
    else:
        print("\n[最终输出] 为空")

    actions = final.get("recommended_actions", [])
    profile = final.get("player_profile", {})

    print(f"\n" + "-" * 40)
    print(f"玩家画像: playstyle={profile.get('playstyle')}, engagement={profile.get('engagement_level')}")
    print(f"推荐行动: {len(actions)} 条")
    for i, action in enumerate(actions, 1):
        print(f"  {i}. [{action.get('priority')}] {action.get('target')}")
        print(f"     类型: {action.get('action_type')}, 置信度: {action.get('confidence')}")
    print(f"\n总耗时: {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
