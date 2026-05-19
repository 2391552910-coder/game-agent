"""RobotGateway 回调客户端。"""

from datetime import UTC, datetime
from typing import Any

import httpx


class RobotGatewayCallbackSkipped(Exception):  # noqa: N818
    """RobotGateway callback 因未配置而跳过。"""


class RobotGatewayCallbackError(Exception):
    """RobotGateway callback 发送失败。"""


def build_robotgateway_callback_payload(
    *,
    tenant_id: str,
    user_id: str,
    snapshot: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, Any]:
    """构造发送给 RobotGateway 的分析完成 payload。"""
    return {
        "event_type": "analysis.completed",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "snapshot": snapshot,
        "analysis": output,
    }


def build_robotgateway_callback_headers(api_key: str | None) -> dict[str, str]:
    """构造 RobotGateway callback 请求头。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Callback-API-Key"] = api_key
    return headers


async def send_robotgateway_analysis_callback(
    *,
    callback_url: str | None,
    api_key: str | None,
    timeout_seconds: float,
    tenant_id: str,
    user_id: str,
    snapshot: dict[str, Any],
    output: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """向 RobotGateway 发送玩家分析完成回调。"""
    if not callback_url:
        raise RobotGatewayCallbackSkipped("RobotGateway callback URL is not configured")

    payload = build_robotgateway_callback_payload(
        tenant_id=tenant_id,
        user_id=user_id,
        snapshot=snapshot,
        output=output,
    )
    headers = build_robotgateway_callback_headers(api_key)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.post(callback_url, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RobotGatewayCallbackError(str(exc)) from exc
