"""
游戏服务器 Webhook 端点。

接收玩家在线/离线事件，触发或取消分析流程。
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class PlayerEvent(BaseModel):
    user_id: str = Field(..., description="玩家 ID")
    event_type: str = Field(..., description="事件类型: online / offline")
    timestamp: float = Field(..., description="事件时间戳")
    snapshot: dict | None = Field(default=None, description="玩家快照数据（可选）")


@router.post("/player-event")
async def handle_player_event(event: PlayerEvent, request: Request):
    """处理玩家在线/离线事件。"""
    tenant_id = request.state.tenant_id

    if event.event_type == "offline":
        from src.core.scheduler.triggers import schedule_offline_analysis

        run_id = await schedule_offline_analysis(
            user_id=event.user_id,
            tenant_id=tenant_id,
        )
        if run_id is None:
            return {"status": "debounced", "user_id": event.user_id}
        return {"status": "scheduled", "user_id": event.user_id, "flow_run_id": run_id}

    elif event.event_type == "online":
        from src.core.scheduler.triggers import cancel_offline_analysis

        await cancel_offline_analysis(user_id=event.user_id)
        return {"status": "cancelled", "user_id": event.user_id}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知事件类型: {event.event_type}",
        )
