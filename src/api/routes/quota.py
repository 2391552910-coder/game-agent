"""配额查询端点。"""

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

from src.core.infrastructure.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/usage")
async def get_quota_usage(request: Request):
    """查看当前租户的配额使用情况。"""
    tenant_id = request.state.tenant_id

    async with get_session() as session:
        row = await session.execute(
            text("""
                SELECT q.monthly_limit, q.used, q.period_start, q.period_end
                FROM quotas q
                WHERE q.tenant_id = :tenant_id
                ORDER BY q.period_start DESC
                LIMIT 1
            """),
            {"tenant_id": tenant_id},
        )
        result = row.first()

    if not result:
        return {"detail": "未找到配额信息"}

    usage_pct = result.used / result.monthly_limit if result.monthly_limit > 0 else 0

    return {
        "monthly_limit": result.monthly_limit,
        "used": result.used,
        "remaining": result.monthly_limit - result.used,
        "usage_percent": f"{usage_pct:.1%}",
        "period_start": result.period_start.isoformat(),
        "period_end": result.period_end.isoformat(),
    }
