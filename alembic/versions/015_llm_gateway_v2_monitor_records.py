"""Persist replayable LLM Gateway v2 monitoring and audit records.

Revision ID: 015
Revises: 014
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_gateway_decisions",
        sa.Column("response_body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "model_calls",
        "usage_reported_calls",
        "usage_missing_calls",
    ):
        op.add_column("llm_gateway_decisions", sa.Column(name, sa.Integer(), nullable=True))
    op.create_table(
        "llm_gateway_monitor_records",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("gateway_id", sa.String(length=128), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("event_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("decision_id", sa.String(length=128), nullable=True),
        sa.Column("record_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")
        ),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("request_body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("error_stage", sa.String(length=64), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.String(length=512), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("model_calls", sa.Integer(), nullable=True),
        sa.Column("usage_reported_calls", sa.Integer(), nullable=True),
        sa.Column("usage_missing_calls", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_gateway_monitor_records"),
        sa.CheckConstraint(
            "record_type IN ('event', 'decision', 'skill', 'chat', 'error')",
            name="ck_llm_gateway_monitor_records_type",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound', 'system')",
            name="ck_llm_gateway_monitor_records_direction",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_llm_gateway_monitor_records_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_llm_gateway_monitor_records_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_llm_gateway_monitor_records_total_tokens_nonnegative",
        ),
    )
    op.create_index(
        "ix_llm_gateway_monitor_records_cursor",
        "llm_gateway_monitor_records",
        ["id"],
    )
    op.create_index(
        "ix_llm_gateway_monitor_records_gateway_time",
        "llm_gateway_monitor_records",
        ["gateway_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_gateway_monitor_records_gateway_time", table_name="llm_gateway_monitor_records")
    op.drop_index("ix_llm_gateway_monitor_records_cursor", table_name="llm_gateway_monitor_records")
    op.drop_table("llm_gateway_monitor_records")
    for name in (
        "usage_missing_calls",
        "usage_reported_calls",
        "model_calls",
        "total_tokens",
        "output_tokens",
        "input_tokens",
        "response_body_json",
    ):
        op.drop_column("llm_gateway_decisions", name)
