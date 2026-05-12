"""LLM Provider 管理端点（管理员）。"""

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from src.core.infrastructure.db import get_session
from src.core.llm.models import (
    LLMProviderCreate,
    LLMProviderResponse,
    LLMProviderUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(request: Request) -> None:
    """验证管理员权限。"""
    is_admin = getattr(request.state, "is_admin", False)
    if not is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")


@router.get("", response_model=list[LLMProviderResponse])
async def list_providers(request: Request):
    """列出所有 LLM Provider（隐藏 api_key）。"""
    _require_admin(request)

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT id, name, provider, model, base_url, weight, is_active, model_type,
                       created_at, updated_at
                FROM llm_providers
                ORDER BY model_type, weight DESC
            """),
        )
        rows = result.fetchall()

    return [
        LLMProviderResponse(
            id=str(row.id),
            name=row.name,
            provider=row.provider,
            model=row.model,
            base_url=row.base_url,
            weight=row.weight,
            is_active=row.is_active,
            model_type=row.model_type,
            provider_type=row.provider_type,
            max_tokens=row.max_tokens,
            timeout=row.timeout,
            extra_params=row.extra_params or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.post("", response_model=LLMProviderResponse, status_code=201)
async def create_provider(req: LLMProviderCreate, request: Request):
    """添加 LLM Provider。"""
    _require_admin(request)

    async with get_session() as session:
        result = await session.execute(
            text("""
                INSERT INTO llm_providers (name, provider, model, api_key, base_url, weight, model_type)
                VALUES (:name, :provider, :model, :api_key, :base_url, :weight, :model_type)
                RETURNING id, name, provider, model, base_url, weight, is_active, model_type,
                          created_at, updated_at
            """),
            req.model_dump(),
        )
        row = result.fetchone()

    _invalidate_balancer_cache()
    logger.info("[providers] 创建 provider: %s (%s)", req.name, row.id)

    return LLMProviderResponse(
        id=str(row.id),
        name=row.name,
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
        weight=row.weight,
        is_active=row.is_active,
        model_type=row.model_type,
        provider_type=row.provider_type,
        max_tokens=row.max_tokens,
        timeout=row.timeout,
        extra_params=row.extra_params or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.put("/{provider_id}", response_model=LLMProviderResponse)
async def update_provider(provider_id: str, req: LLMProviderUpdate, request: Request):
    """更新 LLM Provider（部分更新）。"""
    _require_admin(request)

    # 构建动态 SET 子句
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    set_clauses = []
    params: dict = {"pid": provider_id}
    for key, value in updates.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = value

    set_clauses.append("updated_at = NOW()")
    set_sql = ", ".join(set_clauses)

    async with get_session() as session:
        result = await session.execute(
            text(f"""
                UPDATE llm_providers SET {set_sql}
                WHERE id = :pid
                RETURNING id, name, provider, model, base_url, weight, is_active, model_type,
                          created_at, updated_at
            """),
            params,
        )
        row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Provider 不存在")

    _invalidate_balancer_cache()
    logger.info("[providers] 更新 provider: %s", provider_id[:8])

    return LLMProviderResponse(
        id=str(row.id),
        name=row.name,
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
        weight=row.weight,
        is_active=row.is_active,
        model_type=row.model_type,
        provider_type=row.provider_type,
        max_tokens=row.max_tokens,
        timeout=row.timeout,
        extra_params=row.extra_params or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(provider_id: str, request: Request):
    """删除 LLM Provider（软删除，设为不活跃）。"""
    _require_admin(request)

    async with get_session() as session:
        result = await session.execute(
            text("""
                UPDATE llm_providers SET is_active = FALSE, updated_at = NOW()
                WHERE id = :pid
            """),
            {"pid": provider_id},
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Provider 不存在")

    _invalidate_balancer_cache()
    logger.info("[providers] 软删除 provider: %s", provider_id[:8])


def _invalidate_balancer_cache() -> None:
    """操作完成后刷新 balancer 缓存。"""
    from src.core.llm.balancer import balancer

    balancer.invalidate_cache()
