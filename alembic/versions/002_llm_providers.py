"""LLM Provider 负载均衡池

Revision ID: 002
Revises: 001
Create Date: 2026-04-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── LLM Provider 池 ──
    # 存储多个 LLM API provider，用于加权轮询负载均衡
    op.create_table(
        "llm_providers",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("api_key", sa.String(500), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("model_type", sa.String(20), nullable=False, server_default=sa.text("'default'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_llm_providers_model_type", "llm_providers", ["model_type"])
    op.create_index("ix_llm_providers_is_active", "llm_providers", ["is_active"])


def downgrade() -> None:
    op.drop_table("llm_providers")
