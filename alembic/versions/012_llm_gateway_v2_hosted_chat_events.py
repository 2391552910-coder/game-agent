"""Add Gateway-hosted chat event types to the durable v2 inbox.

Revision ID: 012
Revises: 011
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BASE_EVENT_TYPES = (
    "event_type IN ("
    "'session_started', 'observation_updated', 'skill_started', "
    "'skill_finished', 'decision_rejected', 'session_stopped'"
    ")"
)
_HOSTED_CHAT_EVENT_TYPES = (
    "event_type IN ("
    "'session_started', 'observation_updated', 'skill_started', "
    "'skill_finished', 'decision_rejected', 'session_stopped', "
    "'chat_received', 'nearby_friend_chat_requested', 'chat_send_result'"
    ")"
)


def upgrade() -> None:
    op.drop_constraint("ck_llm_gateway_events_event_type", "llm_gateway_events", type_="check")
    op.create_check_constraint(
        "ck_llm_gateway_events_event_type",
        "llm_gateway_events",
        _HOSTED_CHAT_EVENT_TYPES,
    )


def downgrade() -> None:
    op.drop_constraint("ck_llm_gateway_events_event_type", "llm_gateway_events", type_="check")
    op.create_check_constraint(
        "ck_llm_gateway_events_event_type",
        "llm_gateway_events",
        _BASE_EVENT_TYPES,
    )
