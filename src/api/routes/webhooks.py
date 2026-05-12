"""
游戏服务器 Webhook 端点。

接收玩家在线/离线事件，触发或取消分析流程。
接收玩家在线期间的行为事件，写入 session_events 表。
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class PlayerEvent(BaseModel):
    user_id: str = Field(..., description="玩家 ID")
    event_type: str = Field(..., description="事件类型: online / offline / behavior_checkpoint")
    timestamp: float = Field(..., description="事件时间戳")
    snapshot: dict | None = Field(default=None, description="玩家快照数据（可选）")
    # behavior_checkpoint 专用字段
    session_id: str | None = Field(default=None, description="会话 ID（behavior_checkpoint 必填）")
    behavior_event: dict | None = Field(default=None, description="行为事件详情")


@router.post("/player-event")
async def handle_player_event(event: PlayerEvent, request: Request):
    """处理玩家在线/离线/行为事件。"""
    tenant_id = request.state.tenant_id

    if event.event_type == "offline":
        from src.core.scheduler.triggers import schedule_offline_analysis

        run_id = await schedule_offline_analysis(
            user_id=event.user_id,
            tenant_id=tenant_id,
            snapshot=event.snapshot,
        )
        if run_id is None:
            return {"status": "debounced", "user_id": event.user_id}
        return {"status": "scheduled", "user_id": event.user_id, "flow_run_id": run_id}

    elif event.event_type == "online":
        from src.core.scheduler.triggers import cancel_offline_analysis

        await cancel_offline_analysis(user_id=event.user_id)
        return {"status": "cancelled", "user_id": event.user_id}

    elif event.event_type == "behavior_checkpoint":
        if not event.session_id:
            raise HTTPException(status_code=422, detail="behavior_checkpoint 事件必须提供 session_id")

        await _write_behavior_event(
            tenant_id=tenant_id,
            user_id=event.user_id,
            session_id=event.session_id,
            behavior_event=event.behavior_event or {},
            snapshot=event.snapshot,
        )
        return {"status": "recorded", "user_id": event.user_id}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知事件类型: {event.event_type}",
        )


async def _write_behavior_event(
    tenant_id: str,
    user_id: str,
    session_id: str,
    behavior_event: dict,
    snapshot: dict | None,
) -> None:
    """将行为事件写入 session_events 表。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    event_type = behavior_event.get("type", "unknown")
    event_data = behavior_event.get("data")

    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO session_events (
                    tenant_id, user_id, session_id,
                    event_type, event_data, snapshot
                ) VALUES (
                    :tenant_id, :user_id, :session_id,
                    :event_type, :event_data, :snapshot
                )
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": session_id,
                "event_type": event_type,
                "event_data": event_data,
                "snapshot": snapshot,
            },
        )
    logger.debug(
        "[webhook] 行为事件已写入, user_id=%s, session_id=%s, type=%s",
        user_id,
        session_id,
        event_type,
    )
