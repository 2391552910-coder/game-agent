"""session_events 表

Revision ID: 005
Revises: 004
Create Date: 2026-05-12

新增 session_events 表，用于存储玩家在线期间的行为事件序列：
- 按 user_id + session_id 组织，一次在线期间为一个 session
- 离线触发分析时读取最近一次 session 的事件作为意图推断输入
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_events",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="主键",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False, comment="租户 ID"),
        sa.Column("user_id", sa.String(255), nullable=False, comment="玩家 ID"),
        sa.Column(
            "session_id",
            sa.String(64),
            nullable=False,
            comment="会话 ID，同一次在线期间共享同一个 session_id",
        ),
        sa.Column(
            "event_type",
            sa.String(100),
            nullable=False,
            comment="事件类型，如 move / interact / purchase / quest_accept 等",
        ),
        sa.Column(
            "event_data",
            sa.JSON(),
            nullable=True,
            comment="事件详细数据，JSON 格式，内容由游戏服务器决定",
        ),
        sa.Column(
            "snapshot",
            sa.JSON(),
            nullable=True,
            comment="事件发生时的玩家快照（可选，游戏服务器按需附带）",
        ),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="事件发生时间",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="记录创建时间",
        ),
    )

    op.create_index(
        "ix_session_events_tenant_user_session",
        "session_events",
        ["tenant_id", "user_id", "session_id"],
    )
    op.create_index(
        "ix_session_events_occurred_at",
        "session_events",
        ["tenant_id", "user_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_events_occurred_at", table_name="session_events")
    op.drop_index("ix_session_events_tenant_user_session", table_name="session_events")
    op.drop_table("session_events")
