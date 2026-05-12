"""player_intent 表

Revision ID: 006
Revises: 005
Create Date: 2026-05-12

新增 player_intent 表，用于存储每次离线分析的意图推断结果：
- inferred_intent: 本次会话意图推断（完成了什么/放弃了什么/下次想做什么）
- current_goal: 当前正在追求的主目标
- goal_status: 目标状态（active / completed / abandoned / switched）
- goal_progress: 目标完成度（0.0 - 1.0）
- cost_actual / cost_expected: 实际代价 vs 预期代价
- evaluation_result: goal_evaluation 节点的决策结论（continue / downgrade / switch / new）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_intent",
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
            nullable=True,
            comment="关联的会话 ID",
        ),
        sa.Column(
            "inferred_intent",
            sa.JSON(),
            nullable=True,
            comment=(
                "意图推断结果，JSON 格式，包含："
                "completed（完成了什么）、abandoned（放弃了什么）、next（下次想做什么）"
            ),
        ),
        sa.Column(
            "current_goal",
            sa.Text(),
            nullable=True,
            comment="当前主目标描述",
        ),
        sa.Column(
            "goal_type",
            sa.String(100),
            nullable=True,
            comment="目标分类标签，用于记忆系统统计成功率",
        ),
        sa.Column(
            "goal_status",
            sa.String(20),
            nullable=False,
            server_default="active",
            comment="目标状态: active / completed / abandoned / switched",
        ),
        sa.Column(
            "goal_progress",
            sa.Float(),
            nullable=True,
            comment="目标完成度，0.0 - 1.0",
        ),
        sa.Column(
            "cost_expected",
            sa.Float(),
            nullable=True,
            comment="预期代价（金币/时间等，由上次决策估算）",
        ),
        sa.Column(
            "cost_actual",
            sa.Float(),
            nullable=True,
            comment="实际代价（本次离线时统计）",
        ),
        sa.Column(
            "evaluation_result",
            sa.String(20),
            nullable=True,
            comment="代价权衡决策结论: continue / downgrade / switch / new",
        ),
        sa.Column(
            "evaluation_reason",
            sa.Text(),
            nullable=True,
            comment="决策原因说明",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间（对应本次离线分析时间）",
        ),
    )

    op.create_index(
        "ix_player_intent_tenant_user",
        "player_intent",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_player_intent_created_at",
        "player_intent",
        ["tenant_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_player_intent_created_at", table_name="player_intent")
    op.drop_index("ix_player_intent_tenant_user", table_name="player_intent")
    op.drop_table("player_intent")
