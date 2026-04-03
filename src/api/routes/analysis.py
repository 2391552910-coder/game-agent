"""分析结果查询端点。"""

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from src.core.infrastructure.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{user_id}/latest")
async def get_latest_analysis(user_id: str, request: Request):
    """获取玩家最新分析结果。"""
    tenant_id = request.state.tenant_id

    async with get_session() as session:
        row = await session.execute(
            text("""
                SELECT output_json, analyzed_at
                FROM analysis_results
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                ORDER BY analyzed_at DESC
                LIMIT 1
            """),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        result = row.first()

    if not result:
        raise HTTPException(status_code=404, detail="未找到分析结果")

    import json

    return {
        "user_id": user_id,
        "analyzed_at": result.analyzed_at.isoformat(),
        "output": json.loads(result.output_json) if isinstance(result.output_json, str) else result.output_json,
    }


@router.get("/{user_id}/history")
async def get_analysis_history(user_id: str, request: Request, limit: int = 10):
    """获取玩家分析历史。"""
    tenant_id = request.state.tenant_id

    async with get_session() as session:
        rows = await session.execute(
            text("""
                SELECT output_json, analyzed_at
                FROM analysis_results
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                ORDER BY analyzed_at DESC
                LIMIT :limit
            """),
            {"tenant_id": tenant_id, "user_id": user_id, "limit": limit},
        )
        results = rows.fetchall()

    import json

    return {
        "user_id": user_id,
        "count": len(results),
        "history": [
            {
                "analyzed_at": r.analyzed_at.isoformat(),
                "output": json.loads(r.output_json) if isinstance(r.output_json, str) else r.output_json,
            }
            for r in results
        ],
    }
