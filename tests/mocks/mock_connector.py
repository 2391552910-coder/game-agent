"""模拟游戏数据连接器。

用于测试环境中替代真实游戏数据库连接。
实现 fetch_player_snapshot 函数，返回符合 PlayerSnapshot TypedDict 的数据。
"""

import time
from typing import Any

from src.game_specific.connector import (
    DatabaseError,
    PlayerNotFoundError,
    PlayerSnapshot,
)

# 导入玩家工厂
from tests.mocks.player_factory import (
    generate_competitive_player,
    generate_explorer_player,
    generate_casual_player,
    generate_social_player,
    generate_whale_player,
    PLAYER_TYPE_REGISTRY,
)


# 预定义玩家数据库
MOCK_PLAYERS: dict[str, dict[str, Any]] = {}

# 用户ID到玩家类型的映射
USER_TYPE_MAPPING: dict[str, str] = {}

# 特殊用户ID用于测试异常场景
PLAYER_NOT_FOUND_USER = "player_not_found_error"
DATABASE_ERROR_USER = "player_database_error"


def register_mock_player(user_id: str, player_type: str = "casual") -> None:
    """注册一个模拟玩家到内存数据库。

    参数
    ----
    user_id: str
        玩家ID
    player_type: str
        玩家类型，可选: competitive/explorer/social/casual/whale
    """
    generator = PLAYER_TYPE_REGISTRY.get(player_type, generate_casual_player)
    MOCK_PLAYERS[user_id] = generator(user_id)
    USER_TYPE_MAPPING[user_id] = player_type


def register_multiple_players(count: int, prefix: str = "player", player_type: str = "casual") -> list[str]:
    """批量注册模拟玩家。

    参数
    ----
    count: int
        玩家数量
    prefix: str
        玩家ID前缀
    player_type: str
        玩家类型

    返回
    ----
    list[str]
        注册的玩家ID列表
    """
    user_ids = []
    for i in range(count):
        user_id = f"{prefix}_{i:04d}"
        register_mock_player(user_id, player_type)
        user_ids.append(user_id)
    return user_ids


async def fetch_player_snapshot(user_id: str) -> PlayerSnapshot:
    """获取玩家快照数据（模拟实现）。

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
    """
    # 模拟网络延迟
    await _simulate_delay()

    # 测试异常场景
    if user_id == PLAYER_NOT_FOUND_USER:
        raise PlayerNotFoundError(f"玩家不存在: {user_id}")

    if user_id == DATABASE_ERROR_USER:
        raise DatabaseError(f"数据库连接失败: {user_id}")

    # 如果玩家已注册，返回已注册的数据
    if user_id in MOCK_PLAYERS:
        return PlayerSnapshot(MOCK_PLAYERS[user_id])

    # 动态生成玩家数据（默认为 casual 类型）
    player_data = generate_casual_player(user_id)
    MOCK_PLAYERS[user_id] = player_data
    USER_TYPE_MAPPING[user_id] = "casual"

    return PlayerSnapshot(player_data)


async def _simulate_delay(min_delay: float = 0.01, max_delay: float = 0.05) -> None:
    """模拟网络延迟。"""
    import asyncio
    import random

    delay = random.uniform(min_delay, max_delay)
    await asyncio.sleep(delay)


def update_player_data(user_id: str, updates: dict[str, Any]) -> None:
    """更新玩家数据。

    用于测试场景中动态修改玩家状态。

    参数
    ----
    user_id: str
        玩家ID
    updates: dict
        要更新的字段
    """
    if user_id in MOCK_PLAYERS:
        MOCK_PLAYERS[user_id].update(updates)
    else:
        # 如果玩家不存在，先注册再更新
        register_mock_player(user_id)
        MOCK_PLAYERS[user_id].update(updates)


def get_player_data(user_id: str) -> dict[str, Any] | None:
    """获取玩家原始数据（非 TypedDict）。

    用于测试断言。

    参数
    ----
    user_id: str
        玩家ID

    返回
    ----
    dict | None
        玩家数据，不存在返回 None
    """
    return MOCK_PLAYERS.get(user_id)


def clear_mock_players() -> None:
    """清空所有模拟玩家数据。

    用于测试隔离。
    """
    MOCK_PLAYERS.clear()
    USER_TYPE_MAPPING.clear()


# 预注册一些测试玩家
def _init_default_players() -> None:
    """初始化默认测试玩家。"""
    # 竞技型玩家
    register_mock_player("player_competitive_001", "competitive")
    register_mock_player("player_competitive_002", "competitive")

    # 探索型玩家
    register_mock_player("player_explorer_001", "explorer")
    register_mock_player("player_explorer_002", "explorer")

    # 社交型玩家
    register_mock_player("player_social_001", "social")

    # 休闲玩家
    register_mock_player("player_casual_001", "casual")
    register_mock_player("player_casual_002", "casual")

    # 大R玩家
    register_mock_player("player_whale_001", "whale")


# 模块加载时初始化默认玩家
_init_default_players()
