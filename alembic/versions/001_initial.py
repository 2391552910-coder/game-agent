"""初始数据库表结构

Revision ID: 001
Revises:
Create Date: 2026-04-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 租户表 ──
    # 每个接入方（游戏服务器）对应一个租户，通过 api_key 认证
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.String(255), nullable=False, unique=True),
        sa.Column("api_key", sa.String(255), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_tenants_user_id", "tenants", ["user_id"])
    op.create_index("ix_tenants_api_key", "tenants", ["api_key"])

    # ── Token 配额表 ──
    # 按月度周期记录每个租户的 token 使用量和上限
    op.create_table(
        "quotas",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("monthly_limit", sa.BigInteger(), nullable=False),
        sa.Column("used", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "period_start", name="uq_quotas_tenant_period"),
    )
    op.create_index("ix_quotas_tenant_id", "quotas", ["tenant_id"])

    # ── 分析结果表 ──
    # 存储每次玩家分析的完整输出，tenant_id 非空确保多租户隔离
    op.create_table(
        "analysis_results",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_analysis_results_tenant_user", "analysis_results", ["tenant_id", "user_id"])
    op.create_index("ix_analysis_results_user_id", "analysis_results", ["user_id"])


def downgrade() -> None:
    # 按依赖顺序反向删除，先删有外键的表
    op.drop_table("analysis_results")
    op.drop_table("quotas")
    op.drop_table("tenants")