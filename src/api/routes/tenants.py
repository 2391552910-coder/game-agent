"""租户管理端点。"""

import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from src.config import settings
from src.core.infrastructure.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterRequest(BaseModel):
    user_id: str = Field(..., description="租户关联的用户 ID")


class RegisterResponse(BaseModel):
    tenant_id: str
    api_key: str
    user_id: str


@router.post("/register", response_model=RegisterResponse)
async def register_tenant(req: RegisterRequest):
    """注册新租户。"""
    tenant_id = str(uuid.uuid4())
    api_key = f"gap_{uuid.uuid4().hex[:24]}"

    async with get_session() as session:
        # 检查 user_id 是否已注册
        existing = await session.execute(
            text("SELECT id FROM tenants WHERE user_id = :user_id"),
            {"user_id": req.user_id},
        )
        if existing.first():
            raise HTTPException(status_code=409, detail="该用户已注册")

        # 创建租户
        await session.execute(
            text("""
                INSERT INTO tenants (id, user_id, api_key)
                VALUES (gen_random_uuid(), :user_id, :api_key)
            """),
            {"user_id": req.user_id, "api_key": api_key},
        )

        # 获取刚插入的 tenant_id
        row = await session.execute(
            text("SELECT id FROM tenants WHERE user_id = :user_id"),
            {"user_id": req.user_id},
        )
        tenant_id = str(row.scalar())

        # 创建默认配额
        from datetime import date

        today = date.today()
        period_end = date(today.year, today.month, 1)
        period_end = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)

        await session.execute(
            text("""
                INSERT INTO quotas (tenant_id, monthly_limit, used, period_start, period_end)
                VALUES (:tenant_id, :limit, 0, :start, :end)
            """),
            {
                "tenant_id": tenant_id,
                "limit": settings.default_monthly_tokens,
                "start": today.replace(day=1),
                "end": period_end,
            },
        )

    return RegisterResponse(
        tenant_id=tenant_id,
        api_key=api_key,
        user_id=req.user_id,
    )
