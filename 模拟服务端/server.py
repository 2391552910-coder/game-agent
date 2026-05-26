"""简单 RobotGateway 模拟服务端。"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="Mock RobotGateway", version="1.0.0")


def build_mock_snapshot(user_id: str) -> dict[str, Any]:
    """构造稳定的玩家快照，供 myAgent2 主动拉取。"""
    return {
        "user_id": user_id,
        "player_name": f"模拟玩家_{user_id}",
        "level": 28,
        "vip_level": 2,
        "guild_name": "星境测试公会",
        "guild_id": "guild_mock_001",
        "currencies": {"gold": 128000, "diamond": 860},
        "stamina": 72,
        "exp": 286500,
        "equipment_count": 18,
        "item_count": 64,
        "rare_items": ["基础动作券", "学习加速卡"],
        "play_hours": 42.5,
        "login_days": 16,
        "last_login_at": time.time() - 1800,
        "last_offline_at": time.time() - 300,
        "session_count": 3,
        "online_today_hours": 1.8,
        "main_quest_id": "chapter_learning_03",
        "main_quest_progress": 62,
        "side_quest_count": 12,
        "daily_quest_remaining": 2,
        "pvp_rating": 1350,
        "pve_difficulty": "normal",
        "boss_kill_count": 4,
        "dungeon_clear_count": 18,
        "pvp_win_count": 8,
        "pvp_lose_count": 10,
        "pvp_rank": 3200,
        "pvp_rating_change": -12,
        "friend_count": 9,
        "guild_member_count": 24,
        "chat_message_count": 6,
        "trade_count": 1,
        "game_specific": {
            "source": "mock_robotgateway",
            "current_area": "商业区",
            "target_area": "学习中心",
            "profession": "程序员",
            "recent_activities": ["购物", "学习编程课程", "健身"],
            "bottlenecks": ["学习课程数量偏少", "行动目标不够聚焦"],
        },
    }


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/players/{user_id}/snapshot")
async def get_player_snapshot(user_id: str) -> dict[str, Any]:
    return build_mock_snapshot(user_id)


@app.post("/callbacks/analysis")
async def receive_analysis_callback(request: Request) -> dict[str, str | None]:
    payload = await request.json()
    print("RobotGateway received analysis callback:", flush=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return {"status": "received", "user_id": payload.get("user_id")}
