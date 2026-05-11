"""行动追踪表

Revision ID: 004
Revises: 003
Create Date: 2026-05-11

新增 action_tracking 表，用于监督机制：
- 记录每次分析推荐的可追踪行动
- 存储完成判断所需的目标指标和基准值
- 追踪行动状态变化（tracking / completed / timeout / abandoned）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 action_tracking 表。"""
    op.create_table(
        "action_tracking",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="主键",
        ),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            nullable=False,
            comment="租户 ID，多租户隔离",
        ),
        sa.Column(
            "user_id",
            sa.String(255),
            nullable=False,
            comment="玩家 ID",
        ),
        sa.Column(
            "analysis_id",
            sa.UUID(),
            nullable=True,
            comment="关联的 analysis_results 记录 ID",
        ),
        sa.Column(
            "action_type",
            sa.String(100),
            nullable=False,
            comment="行动类型，如 complete_course / join_guild / pvp_match",
        ),
        sa.Column(
            "action_desc",
            sa.Text(),
            nullable=True,
            comment="行动描述，来自 RecommendedAction.reason",
        ),
        sa.Column(
            "goal_metric",
            sa.String(100),
            nullable=True,
            comment="完成判断指标，对应快照中的字段名，如 learning_courses",
        ),
        sa.Column(
            "goal_value",
            sa.Float(),
            nullable=True,
            comment="目标值，达到此值视为完成",
        ),
        sa.Column(
            "baseline_value",
            sa.Float(),
            nullable=True,
            comment="推荐时的基准值，用于计算进度",
        ),
        sa.Column(
            "expected_hours",
            sa.Integer(),
            nullable=True,
            comment="预计完成所需小时数",
        ),
        sa.Column(
            "deadline",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="截止时间，超时后状态变为 timeout",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="tracking",
            comment="状态: tracking / completed / timeout / abandoned",
        ),
        sa.Column(
            "completed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="完成时间",
        ),
        sa.Column(
            "completion_snapshot",
            sa.JSON(),
            nullable=True,
            comment="完成时的快照关键指标，用于记录完成时的上下文",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="最后更新时间",
        ),
    )

    # 按租户+用户查询追踪记录（最常用）
    op.create_index(
        "ix_action_tracking_tenant_user",
        "action_tracking",
        ["tenant_id", "user_id"],
    )

    # 按状态筛选（查询进行中的追踪）
    op.create_index(
        "ix_action_tracking_status",
        "action_tracking",
        ["tenant_id", "user_id", "status"],
    )

    # 按创建时间排序（查询最近的追踪记录）
    op.create_index(
        "ix_action_tracking_created_at",
        "action_tracking",
        ["tenant_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    """删除 action_tracking 表。"""
    op.drop_index("ix_action_tracking_created_at", table_name="action_tracking")
    op.drop_index("ix_action_tracking_status", table_name="action_tracking")
    op.drop_index("ix_action_tracking_tenant_user", table_name="action_tracking")
    op.drop_table("action_tracking")
