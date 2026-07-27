"""Allow independent terminal events to be processed in one control cycle.

Revision ID: 011
Revises: 010
Create Date: 2026-07-27

Terminal events are reconciled by ``(gateway_id, skill_call_id)`` and may be
processed while an earlier skill event in the same cycle is still running.
The revision-008 partial unique index serialized all processing events in a
cycle, which made that valid terminal path fail with a unique-constraint
violation.  The event worker already enforces the sequencing rules for
non-terminal events, so the index is no longer part of the runtime contract.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "uq_llm_gateway_events_cycle_processing",
        table_name="llm_gateway_events",
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.create_index(
        "uq_llm_gateway_events_cycle_processing",
        "llm_gateway_events",
        ["cycle_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )
