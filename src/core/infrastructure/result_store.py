"""分析结果持久化到 PostgreSQL。"""

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import text

from src.core.infrastructure.db import get_session

logger = logging.getLogger(__name__)


def _snapshot_hash(snapshot: dict) -> str:
    """计算快照哈希，用于去重。"""
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


async def store_analysis(
    tenant_id: str,
    user_id: str,
    snapshot: dict,
    output: dict,
) -> None:
    """存储分析结果。

    Args:
        tenant_id: 租户 ID（非空，多租户隔离）
        user_id: 玩家 ID
        snapshot: 原始快照数据
        output: PlayerAnalysisOutput.model_dump() 的结果
    """
    snap_hash = _snapshot_hash(snapshot)

    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO analysis_results (tenant_id, user_id, snapshot_hash, output_json, analyzed_at)
                VALUES (:tenant_id, :user_id, :snapshot_hash, :output_json, :analyzed_at)
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "snapshot_hash": snap_hash,
                "output_json": json.dumps(output, ensure_ascii=False),
                "analyzed_at": datetime.now(UTC),
            },
        )
    logger.info("分析结果已存储, user_id=%s, hash=%s", user_id, snap_hash[:8])
