"""Persist capacity reservations for Gateway V2 activity decisions.

Revision ID: 014
Revises: 013
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_gateway_decisions",
        sa.Column("activity_capacity_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "llm_gateway_decisions",
        sa.Column("activity_capacity_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "llm_gateway_decisions",
        sa.Column("activity_capacity_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_llm_gateway_decisions_activity_capacity_complete",
        "llm_gateway_decisions",
        """
        (activity_capacity_key IS NULL
         AND activity_capacity_limit IS NULL
         AND activity_capacity_expires_at IS NULL)
        OR
        (activity_capacity_key IS NOT NULL
         AND activity_capacity_limit > 0
         AND activity_capacity_expires_at IS NOT NULL)
        """,
    )
    op.create_index(
        "ix_llm_gateway_decisions_activity_capacity",
        "llm_gateway_decisions",
        ["activity_capacity_key", "activity_capacity_expires_at"],
        postgresql_where=sa.text("activity_capacity_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_llm_gateway_decisions_activity_capacity",
        table_name="llm_gateway_decisions",
    )
    op.drop_constraint(
        "ck_llm_gateway_decisions_activity_capacity_complete",
        "llm_gateway_decisions",
        type_="check",
    )
    op.drop_column("llm_gateway_decisions", "activity_capacity_expires_at")
    op.drop_column("llm_gateway_decisions", "activity_capacity_limit")
    op.drop_column("llm_gateway_decisions", "activity_capacity_key")
