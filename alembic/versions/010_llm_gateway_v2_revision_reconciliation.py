"""Restore the missing LLM Gateway v2 revision marker.

Revision ID: 010
Revises: 009
Create Date: 2026-07-27

The runtime database was already marked as revision 010 while the migration
file was absent. Its V2 schema was compared with a clean revision 009 schema
and confirmed identical, so this reconciliation revision intentionally makes
no schema changes in either direction.
"""

from collections.abc import Sequence

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
