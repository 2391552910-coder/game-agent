"""LLM Gateway v2 durable inbox.

Revision ID: 008
Revises: 007
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_gateway_sessions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("gateway_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("current_generation", sa.BigInteger(), nullable=True),
        sa.Column("fence_version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=24), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_llm_gateway_sessions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_llm_gateway_sessions_tenant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("gateway_id", "session_id", name="uq_llm_gateway_sessions_identity"),
        sa.CheckConstraint(
            "current_generation > 0",
            name="ck_llm_gateway_sessions_current_generation_positive",
        ),
        sa.CheckConstraint(
            "fence_version >= 0",
            name="ck_llm_gateway_sessions_fence_version_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'stopped', 'manual')",
            name="ck_llm_gateway_sessions_status",
        ),
    )
    op.create_index(
        "ix_llm_gateway_sessions_tenant_status",
        "llm_gateway_sessions",
        ["tenant_id", "status"],
    )

    op.create_table(
        "llm_gateway_control_cycles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("runtime_session_id", sa.UUID(), nullable=False),
        sa.Column("gateway_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("control_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("next_event_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("latest_state_version", sa.BigInteger(), nullable=True),
        sa.Column("latest_decision_lease_id", sa.String(length=128), nullable=True),
        sa.Column("latest_decision_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_llm_gateway_control_cycles"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_llm_gateway_cycles_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_session_id"],
            ["llm_gateway_sessions.id"],
            name="fk_llm_gateway_cycles_runtime_session",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "gateway_id",
            "session_id",
            "control_generation",
            name="uq_llm_gateway_cycles_partition",
        ),
        sa.CheckConstraint(
            "control_generation > 0",
            name="ck_llm_gateway_cycles_control_generation_positive",
        ),
        sa.CheckConstraint(
            "next_event_sequence > 0",
            name="ck_llm_gateway_cycles_next_event_sequence_positive",
        ),
        sa.CheckConstraint(
            "latest_state_version >= 0",
            name="ck_llm_gateway_cycles_latest_state_version_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'stopped', 'superseded', 'manual')",
            name="ck_llm_gateway_cycles_status",
        ),
    )
    op.create_index(
        "ix_llm_gateway_cycles_runnable",
        "llm_gateway_control_cycles",
        ["status", "next_event_sequence", "updated_at"],
    )

    op.create_table(
        "llm_gateway_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=False),
        sa.Column("gateway_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("control_generation", sa.BigInteger(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("event_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_fence_version", sa.BigInteger(), nullable=True),
        sa.Column("lock_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("error_stage", sa.String(length=64), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_llm_gateway_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_llm_gateway_events_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["llm_gateway_control_cycles.id"],
            name="fk_llm_gateway_events_cycle",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("gateway_id", "event_id", name="uq_llm_gateway_events_identity"),
        sa.UniqueConstraint(
            "gateway_id",
            "session_id",
            "control_generation",
            "event_sequence",
            name="uq_llm_gateway_events_partition_sequence",
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'session_started', 'observation_updated', 'skill_started', "
            "'skill_finished', 'decision_rejected', 'session_stopped'"
            ")",
            name="ck_llm_gateway_events_event_type",
        ),
        sa.CheckConstraint(
            "event_sequence > 0",
            name="ck_llm_gateway_events_event_sequence_positive",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'processing', 'succeeded', 'retryable_failed', "
            "'dead_letter', 'manual', 'superseded'"
            ")",
            name="ck_llm_gateway_events_status",
        ),
        sa.CheckConstraint(
            "((event_type = 'session_started' AND event_sequence = 1) "
            "OR (event_type <> 'session_started' AND event_sequence > 1))",
            name="ck_llm_gateway_events_session_started_sequence",
        ),
    )
    op.create_index(
        "ix_llm_gateway_events_due",
        "llm_gateway_events",
        ["status", "next_attempt_at", "received_at"],
    )
    op.create_index(
        "uq_llm_gateway_events_cycle_processing",
        "llm_gateway_events",
        ["cycle_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_llm_gateway_events_cycle_processing",
        table_name="llm_gateway_events",
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.drop_index("ix_llm_gateway_events_due", table_name="llm_gateway_events")
    op.drop_table("llm_gateway_events")

    op.drop_index("ix_llm_gateway_cycles_runnable", table_name="llm_gateway_control_cycles")
    op.drop_table("llm_gateway_control_cycles")

    op.drop_index("ix_llm_gateway_sessions_tenant_status", table_name="llm_gateway_sessions")
    op.drop_table("llm_gateway_sessions")
