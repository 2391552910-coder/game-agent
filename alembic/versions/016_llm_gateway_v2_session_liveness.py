"""Track the last admitted event for Gateway v2 session liveness."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_gateway_sessions",
        sa.Column(
            "last_event_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_llm_gateway_sessions_liveness",
        "llm_gateway_sessions",
        ["status", "last_event_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_gateway_sessions_liveness", table_name="llm_gateway_sessions")
    op.drop_column("llm_gateway_sessions", "last_event_at")
