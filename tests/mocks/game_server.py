"""游戏服务器 Webhook 模拟器。

用于模拟游戏服务器发送玩家事件到平台 Webhook 端点。
"""

import asyncio
import time
from typing import Any

import httpx


# 默认平台配置
DEFAULT_PLATFORM_URL = "http://localhost:8000"
DEFAULT_WEBHOOK_PATH = "/webhooks/player-event"


async def send_player_event(
    user_id: str,
    event_type: str,  # "online" | "offline"
    api_key: str,
    base_url: str = DEFAULT_PLATFORM_URL,
    timestamp: float | None = None,
    snapshot: dict[str, Any] | None = None,
) -> httpx.Response:
    """发送玩家事件到平台 Webhook。

    参数
    ----
    user_id: str
        玩家ID
    event_type: str
        事件类型，"online" 或 "offline"
    api_key: str
        租户 API Key
    base_url: str
        平台基础URL
    timestamp: float | None
        事件时间戳（Unix时间戳），默认为当前时间
    snapshot: dict | None
        玩家快照数据（可选）

    返回
    ----
    httpx.Response
        HTTP 响应对象
    """
    if timestamp is None:
        timestamp = time.time()

    payload = {
        "user_id": user_id,
        "event_type": event_type,
        "timestamp": timestamp,
    }

    if snapshot is not None:
        payload["snapshot"] = snapshot

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}{DEFAULT_WEBHOOK_PATH}",
            json=payload,
            headers=headers,
            timeout=10.0,
        )

    return response


async def send_online_event(
    user_id: str,
    api_key: str,
    base_url: str = DEFAULT_PLATFORM_URL,
    timestamp: float | None = None,
) -> httpx.Response:
    """发送玩家上线事件。

    参数
    ----
    user_id: str
        玩家ID
    api_key: str
        租户 API Key
    base_url: str
        平台基础URL
    timestamp: float | None
        事件时间戳（Unix时间戳），默认为当前时间

    返回
    ----
    httpx.Response
        HTTP 响应对象
    """
    return await send_player_event(
        user_id=user_id,
        event_type="online",
        api_key=api_key,
        base_url=base_url,
        timestamp=timestamp,
    )


async def send_offline_event(
    user_id: str,
    api_key: str,
    base_url: str = DEFAULT_PLATFORM_URL,
    timestamp: float | None = None,
    snapshot: dict[str, Any] | None = None,
) -> httpx.Response:
    """发送玩家离线事件。

    参数
    ----
    user_id: str
        玩家ID
    api_key: str
        租户 API Key
    base_url: str
        平台基础URL
    timestamp: float | None
        事件时间戳（Unix时间戳），默认为当前时间
    snapshot: dict | None
        玩家快照数据（可选）

    返回
    ----
    httpx.Response
        HTTP 响应对象
    """
    return await send_player_event(
        user_id=user_id,
        event_type="offline",
        api_key=api_key,
        base_url=base_url,
        timestamp=timestamp,
        snapshot=snapshot,
    )


async def simulate_player_session(
    user_id: str,
    api_key: str,
    online_duration: int = 60,
    offline_delay: int = 0,
    base_url: str = DEFAULT_PLATFORM_URL,
) -> dict[str, httpx.Response]:
    """模拟玩家完整会话：上线 -> 等待 -> 离线。

    参数
    ----
    user_id: str
        玩家ID
    api_key: str
        租户 API Key
    online_duration: int
        在线时长（秒）
    offline_delay: int
        离线后多久触发分析（秒），0 表示立即触发
    base_url: str
        平台基础URL

    返回
    ----
    dict[str, httpx.Response]
        包含 online 和 offline 响应的字典
    """
    # 发送上线事件
    online_response = await send_online_event(
        user_id=user_id,
        api_key=api_key,
        base_url=base_url,
    )

    # 等待在线时长
    await asyncio.sleep(online_duration)

    # 等待离线延迟
    if offline_delay > 0:
        await asyncio.sleep(offline_delay)

    # 发送离线事件
    offline_response = await send_offline_event(
        user_id=user_id,
        api_key=api_key,
        base_url=base_url,
    )

    return {
        "online": online_response,
        "offline": offline_response,
    }


async def batch_send_events(
    events: list[dict[str, Any]],
    api_key: str,
    base_url: str = DEFAULT_PLATFORM_URL,
    delay: float = 0.1,
) -> list[httpx.Response]:
    """批量发送事件。

    参数
    ----
    events: list[dict]
        事件列表，每个事件包含 user_id, event_type, timestamp, snapshot
    api_key: str
        租户 API Key
    base_url: str
        平台基础URL
    delay: float
        每个事件之间的延迟（秒）

    返回
    ----
    list[httpx.Response]
        响应列表
    """
    responses = []

    for event in events:
        response = await send_player_event(
            user_id=event["user_id"],
            event_type=event["event_type"],
            api_key=api_key,
            base_url=base_url,
            timestamp=event.get("timestamp"),
            snapshot=event.get("snapshot"),
        )
        responses.append(response)

        # 延迟
        if delay > 0:
            await asyncio.sleep(delay)

    return responses


async def simulate_multiple_players(
    player_count: int,
    api_key: str,
    base_url: str = DEFAULT_PLATFORM_URL,
    player_type: str = "casual",
    prefix: str = "test_player",
) -> list[dict[str, httpx.Response]]:
    """模拟多个玩家会话。

    参数
    ----
    player_count: int
        玩家数量
    api_key: str
        租户 API Key
    base_url: str
        平台基础URL
    player_type: str
        玩家类型
    prefix: str
        玩家ID前缀

    返回
    ----
    list[dict[str, httpx.Response]]
        每个玩家的响应列表
    """
    from tests.mocks.mock_connector import register_mock_player

    results = []

    for i in range(player_count):
        user_id = f"{prefix}_{i:04d}"

        # 注册模拟玩家
        register_mock_player(user_id, player_type)

        # 模拟会话
        session_result = await simulate_player_session(
            user_id=user_id,
            api_key=api_key,
            base_url=base_url,
            online_duration=random.randint(10, 60),
            offline_delay=0,
        )

        results.append(session_result)

    return results


# 测试API密钥（来自种子数据）
TEST_API_KEYS = {
    "admin": "gap_test_admin_key_001",
    "alpha": "gap_test_alpha_key_002",
    "beta": "gap_test_beta_key_003",
}


def get_test_api_key(tenant: str = "alpha") -> str:
    """获取测试API密钥。

    参数
    ----
    tenant: str
        租户名称，可选: admin/alpha/beta

    返回
    ----
    str
        API密钥
    """
    return TEST_API_KEYS.get(tenant, TEST_API_KEYS["alpha"])


# 辅助函数
import random  # noqa: E402


def random_player_id() -> str:
    """生成随机玩家ID。"""
    return f"player_{random.randint(100000, 999999)}"


def random_timestamp(days_ago: int = 0) -> float:
    """生成随机时间戳。

    参数
    ----
    days_ago: int
        几天前的时间

    返回
    ----
    float
        Unix时间戳
    """
    now = time.time()
    days_in_seconds = days_ago * 86400
    return now - days_in_seconds - random.randint(0, 86400)
