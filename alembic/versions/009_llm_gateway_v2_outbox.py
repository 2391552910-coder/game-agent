"""LLM Gateway v2 decision outbox and skill-call state.

Revision ID: 009
Revises: 008
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_gateway_decisions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=False),
        sa.Column("source_event_id", sa.UUID(), nullable=False),
        sa.Column("action_tracking_id", sa.UUID(), nullable=True),
        sa.Column("gateway_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("decision_lease_id", sa.String(length=128), nullable=False),
        sa.Column("control_generation", sa.BigInteger(), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("request_body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_body_bytes", postgresql.BYTEA(), nullable=False),
        sa.Column("body_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_fence_version", sa.BigInteger(), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("lock_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_http_status", sa.Integer(), nullable=True),
        sa.Column("response_status", sa.String(length=32), nullable=True),
        sa.Column("response_reason", sa.String(length=256), nullable=True),
        sa.Column("skill_call_id", sa.String(length=128), nullable=True),
        sa.Column("error_stage", sa.String(length=64), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_llm_gateway_decisions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_llm_gateway_decisions_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["llm_gateway_control_cycles.id"],
            name="fk_llm_gateway_decisions_cycle",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["llm_gateway_events.id"],
            name="fk_llm_gateway_decisions_source_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_tracking_id"],
            ["action_tracking.id"],
            name="fk_llm_gateway_decisions_action_tracking",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "gateway_id",
            "decision_id",
            name="uq_llm_gateway_decisions_identity",
        ),
        sa.UniqueConstraint(
            "source_event_id",
            name="uq_llm_gateway_decisions_source_event",
        ),
        sa.UniqueConstraint(
            "gateway_id",
            "decision_lease_id",
            name="uq_llm_gateway_decisions_lease",
        ),
        sa.CheckConstraint(
            "action IN ('call_skill', 'wait', 'no_op', 'stop_hosting')",
            name="ck_llm_gateway_decisions_action",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'planned', 'sending', 'accepted', 'rejected', 'retryable_failed', "
            "'dead_letter', 'cancelled', 'manual'"
            ")",
            name="ck_llm_gateway_decisions_status",
        ),
        sa.CheckConstraint(
            "control_generation > 0",
            name="ck_llm_gateway_decisions_control_generation_positive",
        ),
        sa.CheckConstraint(
            "state_version >= 0",
            name="ck_llm_gateway_decisions_state_version_nonnegative",
        ),
        sa.CheckConstraint(
            "lease_expires_at_ms IS NULL OR lease_expires_at_ms > 0",
            name="ck_llm_gateway_decisions_lease_expiry_positive",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_llm_gateway_decisions_attempt_count_nonnegative",
        ),
    )
    op.create_index(
        "ix_llm_gateway_decisions_due",
        "llm_gateway_decisions",
        ["status", "next_attempt_at", "created_at"],
    )

    op.create_table(
        "llm_gateway_skill_calls",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("decision_row_id", sa.UUID(), nullable=False),
        sa.Column("terminal_event_id", sa.UUID(), nullable=True),
        sa.Column("gateway_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("skill_call_id", sa.String(length=128), nullable=False),
        sa.Column("skill_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("effect_status", sa.String(length=24), nullable=False, server_default=sa.text("'not_applicable'")),
        sa.Column("effect_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_llm_gateway_skill_calls"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_llm_gateway_skill_calls_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decision_row_id"],
            ["llm_gateway_decisions.id"],
            name="fk_llm_gateway_skill_calls_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["terminal_event_id"],
            ["llm_gateway_events.id"],
            name="fk_llm_gateway_skill_calls_terminal_event",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "gateway_id",
            "skill_call_id",
            name="uq_llm_gateway_skill_calls_identity",
        ),
        sa.UniqueConstraint(
            "terminal_event_id",
            name="uq_llm_gateway_skill_calls_terminal_event",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'started', 'succeeded', 'failed', 'cancelled', 'timeout', 'manual')",
            name="ck_llm_gateway_skill_calls_status",
        ),
        sa.CheckConstraint(
            "failure_category IS NULL OR failure_category IN ("
            "'business_rejected', 'transport_failed', 'protocol_failed', 'internal_failed'"
            ")",
            name="ck_llm_gateway_skill_calls_failure_category",
        ),
        sa.CheckConstraint(
            "effect_status IN ('not_applicable', 'pending', 'applied', 'manual')",
            name="ck_llm_gateway_skill_calls_effect_status",
        ),
    )
    op.create_index(
        "ix_llm_gateway_skill_calls_decision",
        "llm_gateway_skill_calls",
        ["decision_row_id"],
    )
    op.create_index(
        "ix_llm_gateway_skill_calls_status",
        "llm_gateway_skill_calls",
        ["tenant_id", "status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_gateway_skill_calls_status", table_name="llm_gateway_skill_calls")
    op.drop_index("ix_llm_gateway_skill_calls_decision", table_name="llm_gateway_skill_calls")
    op.drop_table("llm_gateway_skill_calls")

    op.drop_index("ix_llm_gateway_decisions_due", table_name="llm_gateway_decisions")
    op.drop_table("llm_gateway_decisions")
