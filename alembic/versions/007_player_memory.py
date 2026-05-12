"""player_memory 表

Revision ID: 007
Revises: 006
Create Date: 2026-05-12

新增 player_memory 表，用于存储玩家长期记忆（每个玩家一条记录，upsert 模式）：
- behavior_profile: 行为画像（消费习惯、活跃偏好、在线时长等），每次更新
- goal_history: 目标历史统计（分类统计成功率、平均代价等），目标出现 2 次后写入
- analysis_count: 累计分析次数，用于计算统计均值
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_memory",
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
            "behavior_profile",
            sa.JSON(),
            nullable=True,
            comment=(
                "行为画像，JSON 格式，包含："
                "spend_tendency（消费倾向：high/medium/low）、"
                "avg_spend_per_session（每次会话平均消费）、"
                "preferred_content（偏好内容类型列表）、"
                "avg_session_minutes（平均在线时长分钟数）"
            ),
        ),
        sa.Column(
            "goal_history",
            sa.JSON(),
            nullable=True,
            comment=(
                "目标历史统计，JSON 格式，以 goal_type 为键，值包含："
                "total（总次数）、success（成功次数）、"
                "avg_cost（平均实际代价）、abandon_reasons（放弃原因列表）"
            ),
        ),
        sa.Column(
            "analysis_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="累计分析次数",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="首次创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="最后更新时间",
        ),
    )

    # 每个玩家在每个租户下只有一条记录
    op.create_unique_constraint(
        "uq_player_memory_tenant_user",
        "player_memory",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_player_memory_tenant_user",
        "player_memory",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_player_memory_tenant_user", table_name="player_memory")
    op.drop_constraint("uq_player_memory_tenant_user", "player_memory", type_="unique")
    op.drop_table("player_memory")
