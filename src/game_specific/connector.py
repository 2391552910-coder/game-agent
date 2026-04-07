"""游戏数据接口定义。

本文档定义平台与游戏服务器之间的数据接口规范。
游戏厂商需实现本模块中的函数，将游戏数据库对接到平台。

接口契约
========

平台调用 `fetch_player_snapshot(user_id)` 获取玩家快照，
然后将快照送入 LangGraph Agent 进行分析和推理。

快照是玩家当前状态的「快照」，应该包含：
- 玩家基本信息（ID、名字、等级等）
- 当前进度（任务、成就、段位等）
- 资源状态（货币、物品、背包等）
- 行为统计（在线时长、活跃天数、互动次数等）

重要原则
========

1. 返回字典的键名使用 snake_case（如 `play_hours` 而非 `playHours`）
2. 嵌套层级不超过 2 层（避免 LLM 难以解析）
3. 只返回玩家「当前状态」，不返回历史记录
4. 字段值使用 Python 原生类型（str, int, float, list, dict）
5. 所有字段可选（平台能处理缺失字段，但分析质量会下降）

类型定义
========
"""

from typing import TypedDict


class PlayerSnapshot(TypedDict, total=False):
    """玩家快照数据结构。

    平台通过此结构理解玩家当前状态。
    字段按功能分组，* 表示核心必需字段。

    核心字段（建议必填）:
    --------------------
    user_id: str
        玩家在游戏内的唯一标识
    player_name: str
        玩家昵称或显示名

    基础属性:
    --------
    level: int
        玩家等级/段位
    vip_level: int
        VIP 等级（无则为 0）
    guild_name: str
        公会/战队/联盟名称（无工会则为空字符串）
    guild_id: str
        公会 ID（无工会则为 0 或空字符串）

    资源状态:
    --------
    currencies: dict[str, int]
        货币数量，键为货币名称，值为数量
        例如: {"gold": 10000, "diamond": 500, "honor": 1200}
    stamina: int
        体力/能量/疲劳值
    exp: int
        当前经验值

    背包/物品:
    ----------
    equipment_count: int
        装备数量
    item_count: int
        背包物品总数
    rare_items: list[str]
        稀有物品名称列表（最多 10 个）

    行为统计:
    --------
    play_hours: float
        累计游戏时长（小时）
    login_days: int
        累计登录天数
    last_login_at: float
        最近登录时间（Unix 时间戳，秒）
    last_offline_at: float
        最近离线时间（Unix 时间戳，秒）
    session_count: int
        今日会话次数
    online_today_hours: float
        今日在线时长（小时）

    任务/进度:
    ----------
    main_quest_id: str
        当前主线任务/章节 ID
    main_quest_progress: int
        主线任务进度百分比（0-100）
    side_quest_count: int
        已完成支线任务数量
    daily_quest_remaining: int
        今日剩余日常任务数量

    PVE 统计:
    ---------
    pvp_rating: int
        PVP 评分/段位（天梯分、竞技场分等）
    pve_difficulty: str
        常用 PVE 难度，如 "normal", "hard", "heroic", "mythic"
    boss_kill_count: int
        累计击败世界 boss 数量
    dungeon_clear_count: int
        累计通关副本数量

    PVP 统计:
    ---------
    pvp_win_count: int
        PVP 总胜利场次
    pvp_lose_count: int
        PVP 总失败场次
    pvp_rank: int
        当前 PVP 排名（如果有）
    pvp_rating_change: int
        最近一场 PVP 评分变化（正负值）

    社交/互动:
    ---------
    friend_count: int
        好友数量
    guild_member_count: int
        公会成员数量
    chat_message_count: int
        今日聊天消息数
    trade_count: int
        累计交易/交换次数

    自定义字段:
    ----------
    game_specific: dict
        游戏特定数据，键名由游戏方定义，平台会透传给 LLM
        平台不会解析此字段，仅在 RAG 检索时作为上下文
        例如: {"current_area": "王城", "profession": "骑士", "title": "指挥官"}

    示例
    ----
    MMO 游戏快照:
    {
        "user_id": "player_12345",
        "player_name": "阿尔萨斯",
        "level": 85,
        "vip_level": 12,
        "guild_name": "银色黎明",
        "guild_id": "guild_789",
        "currencies": {"gold": 5000000, "diamond": 3200, "honor": 85000},
        "stamina": 120,
        "exp": 8560000,
        "equipment_count": 48,
        "item_count": 256,
        "rare_items": ["霜之哀伤", "亡灵铠甲"],
        "play_hours": 1250.5,
        "login_days": 890,
        "last_login_at": 1744100000.0,
        "last_offline_at": 1744067200.0,
        "online_today_hours": 3.5,
        "main_quest_id": "chapter_15",
        "main_quest_progress": 75,
        "side_quest_count": 234,
        "daily_quest_remaining": 3,
        "pvp_rating": 2400,
        "pve_difficulty": "mythic",
        "boss_kill_count": 156,
        "dungeon_clear_count": 890,
        "pvp_win_count": 1256,
        "pvp_lose_count": 890,
        "pvp_rank": 150,
        "friend_count": 128,
        "guild_member_count": 45,
        "chat_message_count": 45,
        "trade_count": 34,
        "game_specific": {
            "current_area": "冰冠堡垒",
            "profession": "死亡骑士",
            "title": "巫妖王克星",
            "specialization": "邪恶",
            "artifact_power": 45000,
        }
    }

    SLG 游戏快照:
    {
        "user_id": "player_5678",
        "player_name": "曹操",
        "level": 45,
        "vip_level": 8,
        "guild_name": "魏国联盟",
        "guild_id": "alliance_456",
        "currencies": {"gold": 10000000, "food": 5000000, "wood": 3000000, "stone": 2000000},
        "stamina": 0,
        "exp": 450000,
        "equipment_count": 120,
        "item_count": 340,
        "rare_items": ["青冈剑", "孟德新书"],
        "play_hours": 680.0,
        "login_days": 340,
        "last_login_at": 1744100000.0,
        "last_offline_at": 1744067200.0,
        "online_today_hours": 1.2,
        "main_quest_id": "chapter_8",
        "main_quest_progress": 50,
        "side_quest_count": 89,
        "daily_quest_remaining": 5,
        "pvp_rating": 1850,
        "pve_difficulty": "hard",
        "boss_kill_count": 45,
        "dungeon_clear_count": 234,
        "pvp_win_count": 567,
        "pvp_lose_count": 345,
        "pvp_rank": 89,
        "friend_count": 56,
        "guild_member_count": 28,
        "chat_message_count": 12,
        "trade_count": 89,
        "game_specific": {
            "city_level": 25,
            "population": 500000,
            "building_levels": {"barracks": 20, "academy": 18, "storehouse": 15},
            "technology_levels": {"military": 15, "economy": 12, "defense": 10},
            "march_queue_count": 3,
            "hero_stars": {"曹操": 5, "司马懿": 4, "张辽": 4},
        }
    }

    FPS 游戏快照:
    {
        "user_id": "player_9012",
        "player_name": "ShadowSniper",
        "level": 78,
        "vip_level": 0,
        "guild_name": "",
        "guild_id": "",
        "currencies": {"credits": 50000, "scrap": 12000},
        "stamina": 100,
        "exp": 780000,
        "equipment_count": 25,
        "item_count": 48,
        "rare_items": ["AWP-金色", "刀-蝴蝶"],
        "play_hours": 890.5,
        "login_days": 234,
        "last_login_at": 1744100000.0,
        "last_offline_at": 1744067200.0,
        "online_today_hours": 2.5,
        "main_quest_id": "season_5_battlepass",
        "main_quest_progress": 60,
        "side_quest_count": 45,
        "daily_quest_remaining": 2,
        "pvp_rating": 2850,
        "pve_difficulty": "normal",
        "boss_kill_count": 0,
        "dungeon_clear_count": 0,
        "pvp_win_count": 1234,
        "pvp_lose_count": 1098,
        "pvp_rank": 250,
        "friend_count": 89,
        "guild_member_count": 0,
        "chat_message_count": 156,
        "trade_count": 12,
        "game_specific": {
            "accuracy": 42.5,
            "headshot_rate": 38.2,
            "kd_ratio": 1.85,
            "favorite_weapon": "AWP",
            "play_mode": "competitive",
            "rank_name": "Diamond II",
            "season_wins": 89,
        }
    }
"""

from datetime import datetime
from typing import Any

from src.core.infrastructure.db import get_session


async def fetch_player_snapshot(user_id: str) -> PlayerSnapshot:
    """获取玩家快照数据。

    平台在玩家离线时调用此函数获取当前状态。
    游戏厂商需连接自己的数据库实现此函数。

    参数
    ----
    user_id: str
        玩家在游戏内的唯一标识

    返回
    ----
    PlayerSnapshot
        包含玩家当前状态的字典

    异常
    ----
    PlayerNotFoundError
        当 user_id 不存在时抛出
    DatabaseError
        当数据库连接或查询失败时抛出

    实现示例
    --------
    # MySQL 实现
    import aiomysql

    async def fetch_player_snapshot(user_id: str) -> PlayerSnapshot:
        pool = await aiomysql.create_pool(
            host="game-db.example.com",
            port=3306,
            user="game_app",
            password="xxx",
            db="game_server_1",
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM players WHERE user_id = %s",
                    (user_id,)
                )
                row = await cur.fetchone()
        pool.close()

        if not row:
            raise PlayerNotFoundError(f"玩家不存在: {user_id}")

        return PlayerSnapshot(
            user_id=str(row["user_id"]),
            player_name=row["player_name"],
            level=row["level"],
            vip_level=row["vip_level"] or 0,
            guild_name=row.get("guild_name", ""),
            guild_id=str(row.get("guild_id", "")) or "",
            currencies={
                "gold": row["gold"],
                "diamond": row["diamond"],
                "honor": row["honor"],
            },
            stamina=row["stamina"],
            exp=row["exp"],
            play_hours=row["play_hours"],
            login_days=row["login_days"],
            last_login_at=row["last_login_at"].timestamp(),
            last_offline_at=row["last_offline_at"].timestamp(),
            ...
        )

    # PostgreSQL 实现（如果游戏用 pg）
    async def fetch_player_snapshot(user_id: str) -> PlayerSnapshot:
        async with get_session() as session:
            result = await session.execute(
                text("SELECT * FROM players WHERE user_id = :user_id"),
                {"user_id": user_id}
            )
            row = result.fetchone()

        if not row:
            raise PlayerNotFoundError(f"玩家不存在: {user_id}")

        return PlayerSnapshot(
            user_id=str(row.user_id),
            player_name=row.player_name,
            ...
        )
    """
    raise NotImplementedError(
        "游戏厂商需实现 fetch_player_snapshot 函数。\n"
        "参考 src/game_specific/connector.py 中的文档和示例。"
    )


class PlayerNotFoundError(Exception):
    """玩家不存在异常。"""

    pass


class DatabaseError(Exception):
    """数据库访问异常。"""

    pass


# ─── 平台侧辅助函数（游戏方也可调用） ────────────────────────────────────────


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    """验证快照字段是否完整，返回缺失的推荐字段列表。

    平台在接收到快照后会调用此函数检查字段完整性，
    用于向游戏方反馈哪些字段缺失。

    参数
    ----
    snapshot: dict
        玩家快照字典

    返回
    ----
    list[str]
        缺失的推荐字段名列表（为空表示字段完整）
    """
    recommended_fields = [
        "user_id",
        "player_name",
        "level",
        "play_hours",
        "login_days",
        "currencies",
        "stamina",
        "pvp_rating",
        "pve_difficulty",
    ]

    missing = []
    for field in recommended_fields:
        if field not in snapshot or snapshot[field] is None:
            missing.append(field)

    return missing


def build_game_context(snapshot: dict[str, Any]) -> str:
    """将快照转换为 LLM 友好的自然语言描述。

    用于 RAG 检索时的上下文构建，或作为分析时的补充说明。
    平台会自动调用此函数。

    参数
    ----
    snapshot: dict
        玩家快照字典

    返回
    ----
    str
        自然语言描述
    """
    parts = []

    # 基础信息
    if "player_name" in snapshot:
        parts.append(f"玩家 {snapshot['player_name']}")
    if "level" in snapshot:
        parts.append(f"等级 {snapshot['level']}")
    if "vip_level" in snapshot and snapshot["vip_level"] > 0:
        parts.append(f"VIP {snapshot['vip_level']}")

    # 进度
    if "main_quest_progress" in snapshot:
        parts.append(f"主线进度 {snapshot['main_quest_progress']}%")

    # 资源
    if "currencies" in snapshot and isinstance(snapshot["currencies"], dict):
        currency_str = ", ".join(
            f"{v} {k}" for k, v in list(snapshot["currencies"].items())[:3]
        )
        parts.append(f"资源: {currency_str}")

    # 行为
    if "play_hours" in snapshot:
        parts.append(f"累计游戏 {snapshot['play_hours']:.1f} 小时")
    if "online_today_hours" in snapshot:
        parts.append(f"今日在线 {snapshot['online_today_hours']:.1f} 小时")

    # PVP
    if "pvp_rating" in snapshot:
        parts.append(f"PVP 评分 {snapshot['pvp_rating']}")
    if "pvp_win_count" in snapshot and "pvp_lose_count" in snapshot:
        wins = snapshot["pvp_win_count"]
        losses = snapshot["pvp_lose_count"]
        total = wins + losses
        if total > 0:
            wr = wins / total * 100
            parts.append(f"总场次 {total}, 胜率 {wr:.1f}%")

    # 游戏特定
    if "game_specific" in snapshot and isinstance(snapshot["game_specific"], dict):
        gs = snapshot["game_specific"]
        highlights = []
        for key, value in list(gs.items())[:5]:
            if isinstance(value, (str, int, float)):
                highlights.append(f"{key}={value}")
        if highlights:
            parts.append(" | ".join(highlights))

    return " | ".join(parts) if parts else "玩家数据不足"
