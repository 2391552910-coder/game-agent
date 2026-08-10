"""Persist the activity plan projection for LLM Gateway v2 cycles.

Revision ID: 013
Revises: 012
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_plan_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_goal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_phase", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column(
            "activity_status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'inactive'"),
        ),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_current_step_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column(
            "activity_plan_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_last_event_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "llm_gateway_control_cycles",
        sa.Column("activity_last_event_sequence", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_llm_gateway_cycles_activity_status",
        "llm_gateway_control_cycles",
        "activity_status IN ('inactive', 'active', 'completed', 'paused', 'abandoned')",
    )
    op.create_check_constraint(
        "ck_llm_gateway_cycles_activity_plan_version_nonnegative",
        "llm_gateway_control_cycles",
        "activity_plan_version >= 0",
    )
    op.create_check_constraint(
        "ck_llm_gateway_cycles_activity_last_sequence_positive",
        "llm_gateway_control_cycles",
        "activity_last_event_sequence IS NULL OR activity_last_event_sequence > 0",
    )
    op.create_index(
        "ix_llm_gateway_cycles_activity",
        "llm_gateway_control_cycles",
        ["gateway_id", "session_id", "control_generation", "activity_status"],
    )

    for name, column in (
        ("activity_plan_id", sa.String(length=128)),
        ("activity_step_id", sa.String(length=128)),
        ("activity_phase", sa.String(length=64)),
    ):
        op.add_column("llm_gateway_decisions", sa.Column(name, column, nullable=True))
    op.add_column(
        "llm_gateway_decisions",
        sa.Column("activity_plan_version", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_llm_gateway_decisions_activity_plan_version_nonnegative",
        "llm_gateway_decisions",
        "activity_plan_version IS NULL OR activity_plan_version > 0",
    )
    op.create_index(
        "ix_llm_gateway_decisions_activity",
        "llm_gateway_decisions",
        ["cycle_id", "activity_plan_id", "activity_step_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_gateway_decisions_activity", table_name="llm_gateway_decisions")
    op.drop_constraint(
        "ck_llm_gateway_decisions_activity_plan_version_nonnegative",
        "llm_gateway_decisions",
        type_="check",
    )
    op.drop_column("llm_gateway_decisions", "activity_plan_version")
    op.drop_column("llm_gateway_decisions", "activity_phase")
    op.drop_column("llm_gateway_decisions", "activity_step_id")
    op.drop_column("llm_gateway_decisions", "activity_plan_id")

    op.drop_index("ix_llm_gateway_cycles_activity", table_name="llm_gateway_control_cycles")
    op.drop_constraint(
        "ck_llm_gateway_cycles_activity_last_sequence_positive",
        "llm_gateway_control_cycles",
        type_="check",
    )
    op.drop_constraint(
        "ck_llm_gateway_cycles_activity_plan_version_nonnegative",
        "llm_gateway_control_cycles",
        type_="check",
    )
    op.drop_constraint(
        "ck_llm_gateway_cycles_activity_status",
        "llm_gateway_control_cycles",
        type_="check",
    )
    for name in (
        "activity_last_event_sequence",
        "activity_last_event_id",
        "activity_plan_version",
        "activity_current_step_id",
        "activity_status",
        "activity_phase",
        "activity_plan",
        "activity_goal",
        "activity_plan_id",
    ):
        op.drop_column("llm_gateway_control_cycles", name)
