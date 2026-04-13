"""玩家数据生成工厂。

用于生成各种类型的测试玩家数据，支持多种玩家原型。
"""

import random
import time
from typing import Any


def generate_competitive_player(user_id: str) -> dict[str, Any]:
    """生成竞技型玩家数据。

    特点：高PVP评分，高胜率，冲击排名
    """
    level = random.randint(70, 90)
    vip_level = random.randint(8, 15)
    pvp_rating = random.randint(2200, 3000)
    pvp_win_count = random.randint(1000, 2000)
    pvp_lose_count = random.randint(500, 1000)

    return {
        "user_id": user_id,
        "player_name": f"竞技者_{random.randint(1000, 9999)}",
        "level": level,
        "vip_level": vip_level,
        "guild_name": random.choice(["冠军战队", "竞技殿堂", "王者联盟", ""]),
        "guild_id": f"guild_{random.randint(1, 100)}" if random.random() > 0.2 else "",
        "currencies": {
            "gold": random.randint(5000000, 20000000),
            "diamond": random.randint(5000, 20000),
            "honor": random.randint(50000, 150000),
        },
        "stamina": random.randint(50, 150),
        "exp": level * 100000 + random.randint(0, 99999),
        "equipment_count": random.randint(40, 60),
        "item_count": random.randint(200, 400),
        "rare_items": random.sample(["传说武器", "神话护甲", "神圣饰品", "传奇戒指"], random.randint(1, 3)),
        "play_hours": random.randint(800, 2000),
        "login_days": random.randint(500, 1000),
        "last_login_at": time.time() - random.randint(0, 86400),
        "last_offline_at": time.time() - random.randint(3600, 7200),
        "session_count": random.randint(1, 10),
        "online_today_hours": random.uniform(2, 8),
        "main_quest_id": f"chapter_{random.randint(10, 20)}",
        "main_quest_progress": random.randint(60, 100),
        "side_quest_count": random.randint(200, 500),
        "daily_quest_remaining": random.randint(0, 5),
        "pvp_rating": pvp_rating,
        "pve_difficulty": random.choice(["heroic", "mythic"]),
        "boss_kill_count": random.randint(100, 500),
        "dungeon_clear_count": random.randint(500, 1500),
        "pvp_win_count": pvp_win_count,
        "pvp_lose_count": pvp_lose_count,
        "pvp_rank": random.randint(1, 500),
        "pvp_rating_change": random.randint(-50, 100),
        "friend_count": random.randint(50, 200),
        "guild_member_count": random.randint(20, 100) if random.random() > 0.2 else 0,
        "chat_message_count": random.randint(10, 50),
        "trade_count": random.randint(10, 50),
        "game_specific": {
            "current_area": random.choice(["竞技场", "天梯大厅", "段位赛"]),
            "favorite_mode": "competitive",
            "win_rate": round(pvp_win_count / (pvp_win_count + pvp_lose_count), 3),
            "target_rank": random.randint(1, 100),
        },
    }


def generate_explorer_player(user_id: str) -> dict[str, Any]:
    """生成探索型玩家数据。

    特点：高任务完成数，多区域探索
    """
    level = random.randint(50, 80)
    vip_level = random.randint(3, 8)

    return {
        "user_id": user_id,
        "player_name": f"探险家_{random.randint(1000, 9999)}",
        "level": level,
        "vip_level": vip_level,
        "guild_name": random.choice(["冒险者公会", "探索者联盟", "游侠团", ""]),
        "guild_id": f"guild_{random.randint(1, 100)}" if random.random() > 0.3 else "",
        "currencies": {
            "gold": random.randint(1000000, 5000000),
            "diamond": random.randint(1000, 5000),
            "exploration_points": random.randint(10000, 50000),
        },
        "stamina": random.randint(80, 200),
        "exp": level * 100000 + random.randint(0, 99999),
        "equipment_count": random.randint(30, 50),
        "item_count": random.randint(300, 600),
        "rare_items": random.sample(["探险地图", "指南针", "登山绳", "露营帐篷"], random.randint(2, 4)),
        "play_hours": random.randint(500, 1500),
        "login_days": random.randint(300, 800),
        "last_login_at": time.time() - random.randint(0, 86400),
        "last_offline_at": time.time() - random.randint(3600, 10800),
        "session_count": random.randint(1, 5),
        "online_today_hours": random.uniform(3, 10),
        "main_quest_id": f"exploration_{random.randint(1, 20)}",
        "main_quest_progress": random.randint(30, 90),
        "side_quest_count": random.randint(400, 800),
        "daily_quest_remaining": random.randint(0, 10),
        "pvp_rating": random.randint(1200, 1800),
        "pve_difficulty": random.choice(["normal", "hard", "heroic"]),
        "boss_kill_count": random.randint(200, 800),
        "dungeon_clear_count": random.randint(800, 2000),
        "pvp_win_count": random.randint(200, 500),
        "pvp_lose_count": random.randint(300, 600),
        "pvp_rank": random.randint(500, 2000),
        "pvp_rating_change": random.randint(-30, 50),
        "friend_count": random.randint(100, 300),
        "guild_member_count": random.randint(30, 150) if random.random() > 0.3 else 0,
        "chat_message_count": random.randint(20, 80),
        "trade_count": random.randint(30, 100),
        "game_specific": {
            "areas_explored": random.randint(50, 200),
            "hidden_areas_found": random.randint(5, 30),
            "achievements_unlocked": random.randint(100, 500),
            "current_region": random.choice(["神秘森林", "远古遗迹", "冰封山顶", "沙漠绿洲"]),
        },
    }


def generate_social_player(user_id: str) -> dict[str, Any]:
    """生成社交型玩家数据。

    特点：高公会成员数，高聊天数
    """
    level = random.randint(30, 60)
    vip_level = random.randint(2, 6)

    return {
        "user_id": user_id,
        "player_name": f"交际花_{random.randint(1000, 9999)}",
        "level": level,
        "vip_level": vip_level,
        "guild_name": random.choice(["欢乐谷", "友情联盟", "兄弟会", "姐妹团"]),
        "guild_id": f"guild_{random.randint(1, 100)}",
        "currencies": {
            "gold": random.randint(500000, 2000000),
            "diamond": random.randint(500, 3000),
            "social_points": random.randint(5000, 20000),
        },
        "stamina": random.randint(100, 200),
        "exp": level * 100000 + random.randint(0, 99999),
        "equipment_count": random.randint(20, 40),
        "item_count": random.randint(150, 300),
        "rare_items": random.sample(["社交徽章", "礼物盒", "派对帽", "纪念币"], random.randint(1, 3)),
        "play_hours": random.randint(300, 800),
        "login_days": random.randint(200, 600),
        "last_login_at": time.time() - random.randint(0, 86400),
        "last_offline_at": time.time() - random.randint(1800, 7200),
        "session_count": random.randint(3, 15),
        "online_today_hours": random.uniform(4, 12),
        "main_quest_id": f"social_{random.randint(1, 10)}",
        "main_quest_progress": random.randint(20, 80),
        "side_quest_count": random.randint(100, 300),
        "daily_quest_remaining": random.randint(5, 15),
        "pvp_rating": random.randint(1000, 1600),
        "pve_difficulty": random.choice(["normal", "hard"]),
        "boss_kill_count": random.randint(50, 200),
        "dungeon_clear_count": random.randint(200, 600),
        "pvp_win_count": random.randint(100, 400),
        "pvp_lose_count": random.randint(150, 450),
        "pvp_rank": random.randint(1000, 3000),
        "pvp_rating_change": random.randint(-20, 30),
        "friend_count": random.randint(200, 500),
        "guild_member_count": random.randint(50, 200),
        "chat_message_count": random.randint(100, 500),
        "trade_count": random.randint(50, 200),
        "game_specific": {
            "guild_role": random.choice(["会长", "副会长", "精英", "普通成员"]),
            "events_hosted": random.randint(10, 100),
            "gifts_sent": random.randint(50, 300),
            "current_activity": random.choice(["公会聊天", "组队副本", "社交活动"]),
        },
    }


def generate_casual_player(user_id: str) -> dict[str, Any]:
    """生成休闲玩家数据。

    特点：低在线时长，低等级，休闲玩家
    """
    level = random.randint(10, 40)
    vip_level = random.randint(0, 3)

    return {
        "user_id": user_id,
        "player_name": f"休闲玩家_{random.randint(1000, 9999)}",
        "level": level,
        "vip_level": vip_level,
        "guild_name": random.choice(["新手公会", ""] * 5 + [""]),  # 大多没有公会
        "guild_id": f"guild_{random.randint(1, 100)}" if random.random() > 0.7 else "",
        "currencies": {
            "gold": random.randint(10000, 200000),
            "diamond": random.randint(50, 500),
        },
        "stamina": random.randint(50, 150),
        "exp": level * 100000 + random.randint(0, 99999),
        "equipment_count": random.randint(5, 20),
        "item_count": random.randint(50, 150),
        "rare_items": [],
        "play_hours": random.randint(20, 200),
        "login_days": random.randint(10, 100),
        "last_login_at": time.time() - random.randint(0, 86400 * 3),
        "last_offline_at": time.time() - random.randint(86400, 86400 * 7),
        "session_count": random.randint(1, 3),
        "online_today_hours": random.uniform(0.5, 3),
        "main_quest_id": f"chapter_{random.randint(1, 5)}",
        "main_quest_progress": random.randint(10, 60),
        "side_quest_count": random.randint(10, 50),
        "daily_quest_remaining": random.randint(5, 20),
        "pvp_rating": random.randint(800, 1400),
        "pve_difficulty": random.choice(["easy", "normal"]),
        "boss_kill_count": random.randint(5, 50),
        "dungeon_clear_count": random.randint(20, 100),
        "pvp_win_count": random.randint(10, 100),
        "pvp_lose_count": random.randint(20, 150),
        "pvp_rank": random.randint(2000, 5000),
        "pvp_rating_change": random.randint(-10, 20),
        "friend_count": random.randint(10, 50),
        "guild_member_count": random.randint(10, 50) if random.random() > 0.7 else 0,
        "chat_message_count": random.randint(5, 30),
        "trade_count": random.randint(5, 20),
        "game_specific": {
            "login_frequency": random.choice(["daily", "weekly", "monthly"]),
            "preferred_activity": random.choice(["日常任务", "挂机", "聊天"]),
        },
    }


def generate_whale_player(user_id: str) -> dict[str, Any]:
    """生成大R玩家数据。

    特点：高VIP等级，高资源消耗
    """
    level = random.randint(60, 90)
    vip_level = random.randint(12, 20)

    return {
        "user_id": user_id,
        "player_name": f"尊贵VIP_{random.randint(1000, 9999)}",
        "level": level,
        "vip_level": vip_level,
        "guild_name": random.choice(["精英联盟", "王者之师", "霸主公会"]),
        "guild_id": f"guild_{random.randint(1, 100)}",
        "currencies": {
            "gold": random.randint(50000000, 200000000),
            "diamond": random.randint(50000, 200000),
            "honor": random.randint(100000, 500000),
            "premium_currency": random.randint(10000, 50000),
        },
        "stamina": random.randint(150, 300),
        "exp": level * 100000 + random.randint(0, 99999),
        "equipment_count": random.randint(50, 80),
        "item_count": random.randint(500, 1000),
        "rare_items": random.sample(
            ["神话武器", "传说护甲", "神圣坐骑", "限定翅膀", "专属称号", "至尊徽章"],
            random.randint(4, 6),
        ),
        "play_hours": random.randint(1000, 3000),
        "login_days": random.randint(600, 1200),
        "last_login_at": time.time() - random.randint(0, 3600),
        "last_offline_at": time.time() - random.randint(600, 3600),
        "session_count": random.randint(5, 20),
        "online_today_hours": random.uniform(5, 15),
        "main_quest_id": f"chapter_{random.randint(15, 25)}",
        "main_quest_progress": random.randint(70, 100),
        "side_quest_count": random.randint(500, 1000),
        "daily_quest_remaining": 0,  # 通常都会完成
        "pvp_rating": random.randint(2000, 3200),
        "pve_difficulty": "mythic",
        "boss_kill_count": random.randint(500, 2000),
        "dungeon_clear_count": random.randint(2000, 5000),
        "pvp_win_count": random.randint(2000, 5000),
        "pvp_lose_count": random.randint(1000, 2500),
        "pvp_rank": random.randint(1, 200),
        "pvp_rating_change": random.randint(50, 150),
        "friend_count": random.randint(200, 500),
        "guild_member_count": random.randint(50, 150),
        "chat_message_count": random.randint(50, 200),
        "trade_count": random.randint(100, 500),
        "game_specific": {
            "total_spent": random.randint(100000, 1000000),
            "monthly_pass": True,
            "battle_pass_level": random.randint(80, 200),
            "exclusive_items": random.randint(20, 50),
            "vip_benefits": ["专属客服", "优先匹配", "特殊称号", "独有活动"],
        },
    }


# 玩家类型注册表
PLAYER_TYPE_REGISTRY: dict[str, callable] = {
    "competitive": generate_competitive_player,
    "explorer": generate_explorer_player,
    "social": generate_social_player,
    "casual": generate_casual_player,
    "whale": generate_whale_player,
}


def generate_player(user_id: str, player_type: str = "casual") -> dict[str, Any]:
    """根据类型生成玩家数据。

    参数
    ----
    user_id: str
        玩家ID
    player_type: str
        玩家类型，可选: competitive/explorer/social/casual/whale

    返回
    ----
    dict
        玩家数据
    """
    generator = PLAYER_TYPE_REGISTRY.get(player_type, generate_casual_player)
    return generator(user_id)


def generate_player_sequence(
    user_id: str,
    days: int,
    player_type: str = "casual",
) -> list[dict[str, Any]]:
    """生成玩家时间序列数据。

    模拟玩家多天的状态变化。

    参数
    ----
    user_id: str
        玩家ID
    days: int
        天数
    player_type: str
        玩家类型

    返回
    ----
    list[dict]
        每天的玩家数据列表
    """
    sequence = []
    base_data = generate_player(user_id, player_type)

    for day in range(days):
        daily_data = base_data.copy()
        daily_data["user_id"] = f"{user_id}_day{day}"
        daily_data["play_hours"] = base_data["play_hours"] + day * random.randint(1, 5)
        daily_data["login_days"] = base_data["login_days"] + day
        daily_data["level"] = base_data["level"] + day // 10  # 每10天升一级
        daily_data["exp"] = daily_data["level"] * 100000 + random.randint(0, 99999)

        # 调整PVP评分
        rating_change = random.randint(-20, 30)
        daily_data["pvp_rating"] = max(800, base_data["pvp_rating"] + rating_change * day)
        daily_data["pvp_win_count"] = base_data["pvp_win_count"] + day * random.randint(2, 10)
        daily_data["pvp_lose_count"] = base_data["pvp_lose_count"] + day * random.randint(1, 8)

        sequence.append(daily_data)

    return sequence
