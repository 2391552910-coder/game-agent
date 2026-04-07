"""游戏数据接入层。

游戏厂商在此目录下实现与平台的数据对接。
核心接口定义在 connector.py 中。
"""

from src.game_specific.connector import (
    PlayerSnapshot,
    PlayerNotFoundError,
    DatabaseError,
    fetch_player_snapshot,
    validate_snapshot,
    build_game_context,
)

__all__ = [
    "PlayerSnapshot",
    "PlayerNotFoundError",
    "DatabaseError",
    "fetch_player_snapshot",
    "validate_snapshot",
    "build_game_context",
]
