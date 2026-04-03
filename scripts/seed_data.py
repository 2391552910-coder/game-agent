"""种子数据脚本。

插入测试用租户、配额、分析结果，用于开发环境功能验证。

用法:
    uv run python scripts/seed_data.py
"""

import asyncio
import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import text

from src.core.infrastructure.db import get_session

# ── 测试数据 ──

TENANTS = [
    {
        "user_id": "admin_001",
        "api_key": "gap_test_admin_key_001",
        "is_active": True,
        "is_admin": True,
    },
    {
        "user_id": "game_server_alpha",
        "api_key": "gap_test_alpha_key_002",
        "is_active": True,
        "is_admin": False,
    },
    {
        "user_id": "game_server_beta",
        "api_key": "gap_test_beta_key_003",
        "is_active": True,
        "is_admin": False,
    },
    {
        "user_id": "disabled_tenant",
        "api_key": "gap_test_disabled_key_004",
        "is_active": False,
        "is_admin": False,
    },
]

# 配额数据会根据 tenant 动态生成

PLAYERS = [
    "player_001",
    "player_002",
    "player_003",
]

ANALYSIS_RESULTS = [
    # player_001 - 两次分析历史
    {
        "user_id": "player_001",
        "snapshot": {"user_id": "player_001", "player_name": "张三", "level": 42, "guild": "星辰阁", "stats": {"play_hours": 320, "quests_completed": 89, "pvp_wins": 156}},
        "output": {
            "player_profile": {"playstyle": "competitive", "current_goal": "冲击天梯前100", "bottlenecks": ["装备评分不足", "缺少传说武器"], "resource_status": "scarce", "play_time_pattern": "工作日晚间活跃，周末全天", "engagement_level": "high"},
            "recommended_actions": [
                {"action_type": "preparation", "target": "参与公会副本获取传说武器材料", "priority": "high", "reason": "传说武器是冲击天梯的硬性门槛", "confidence": 0.92, "rule_source": "装备系统规则", "payload": {"dungeon": "龙巢深渊", "difficulty": "heroic"}},
                {"action_type": "quest", "target": "完成每日竞技场任务积累积分", "priority": "medium", "reason": "积分可兑换强化材料", "confidence": 0.85, "rule_source": "竞技场规则", "payload": {}},
            ],
        },
        "hours_ago": 2,
    },
    {
        "user_id": "player_001",
        "snapshot": {"user_id": "player_001", "player_name": "张三", "level": 40, "guild": "星辰阁", "stats": {"play_hours": 300, "quests_completed": 82, "pvp_wins": 140}},
        "output": {
            "player_profile": {"playstyle": "competitive", "current_goal": "提升PVP胜率", "bottlenecks": ["技能连招不熟练"], "resource_status": "normal", "play_time_pattern": "工作日晚间活跃", "engagement_level": "high"},
            "recommended_actions": [
                {"action_type": "exploration", "target": "练习技能连招组合", "priority": "high", "reason": "连招熟练度直接影响PVP胜率", "confidence": 0.88, "rule_source": "战斗系统规则", "payload": {}},
            ],
        },
        "hours_ago": 26,
    },
    # player_002 - 一次分析
    {
        "user_id": "player_002",
        "snapshot": {"user_id": "player_002", "player_name": "李四", "level": 15, "guild": None, "stats": {"play_hours": 45, "quests_completed": 20, "pvp_wins": 3}},
        "output": {
            "player_profile": {"playstyle": "explorer", "current_goal": "探索世界地图", "bottlenecks": ["等级太低无法进入高级区域"], "resource_status": "normal", "play_time_pattern": "周末偶尔上线", "engagement_level": "medium"},
            "recommended_actions": [
                {"action_type": "quest", "target": "完成主线任务提升等级", "priority": "high", "reason": "主线任务经验收益最高", "confidence": 0.95, "rule_source": "升级规则", "payload": {}},
                {"action_type": "social", "target": "加入活跃公会获取经验加成", "priority": "medium", "reason": "公会加成可提升30%经验获取", "confidence": 0.80, "rule_source": "公会系统规则", "payload": {}},
            ],
        },
        "hours_ago": 8,
    },
]


async def seed():
    now = datetime.now(timezone.utc)
    tenant_ids = {}

    async with get_session() as session:
        # ── 清空旧数据 ──
        await session.execute(text("DELETE FROM analysis_results"))
        await session.execute(text("DELETE FROM quotas"))
        await session.execute(text("DELETE FROM tenants"))

        # ── 插入租户 ──
        for t in TENANTS:
            tid = str(uuid.uuid4())
            tenant_ids[t["user_id"]] = tid
            await session.execute(
                text("""
                    INSERT INTO tenants (id, user_id, api_key, is_active, is_admin)
                    VALUES (:id, :user_id, :api_key, :is_active, :is_admin)
                """),
                {
                    "id": tid,
                    "user_id": t["user_id"],
                    "api_key": t["api_key"],
                    "is_active": t["is_active"],
                    "is_admin": t["is_admin"],
                },
            )
            print(f"  [tenant] {t['user_id']} -> api_key={t['api_key']}")

        # ── 插入配额 ──
        today = date.today()
        period_start = today.replace(day=1)
        if today.month == 12:
            period_end = date(today.year + 1, 1, 1)
        else:
            period_end = date(today.year, today.month + 1, 1)

        for t in TENANTS:
            if not t["is_active"]:
                continue
            tid = tenant_ids[t["user_id"]]
            # admin 用的多一些
            used = 5_200_000 if t["is_admin"] else 1_800_000
            await session.execute(
                text("""
                    INSERT INTO quotas (tenant_id, monthly_limit, used, period_start, period_end)
                    VALUES (:tid, :limit, :used, :start, :end)
                """),
                {
                    "tid": tid,
                    "limit": 40_000_000,
                    "used": used,
                    "start": period_start,
                    "end": period_end,
                },
            )
            print(f"  [quota] {t['user_id']} -> {used/40_000_000:.1%}")

        # ── 插入分析结果 ──
        # 使用 alpha 租户
        alpha_tid = tenant_ids["game_server_alpha"]
        for r in ANALYSIS_RESULTS:
            snapshot_json = json.dumps(r["snapshot"], ensure_ascii=False)
            snap_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
            output_json = json.dumps(r["output"], ensure_ascii=False)
            analyzed_at = datetime.fromtimestamp(now.timestamp() - r["hours_ago"] * 3600, tz=timezone.utc)

            await session.execute(
                text("""
                    INSERT INTO analysis_results (tenant_id, user_id, snapshot_hash, output_json, analyzed_at)
                    VALUES (:tid, :uid, :hash, :output, :at)
                """),
                {
                    "tid": alpha_tid,
                    "uid": r["user_id"],
                    "hash": snap_hash,
                    "output": output_json,
                    "at": analyzed_at,
                },
            )
            print(f"  [result] {r['user_id']} -> {r['hours_ago']}h ago")

    print(f"\n种子数据插入完成:")
    print(f"  租户: {len(TENANTS)}")
    print(f"  分析结果: {len(ANALYSIS_RESULTS)}")


if __name__ == "__main__":
    asyncio.run(seed())
