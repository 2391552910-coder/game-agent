"""LLM provider_type 字段扩展

Revision ID: 003
Revises: 002
Create Date: 2026-04-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """扩展 llm_providers 表，添加多提供商支持。"""
    # 添加 provider_type 字段（默认 openai 以保持向后兼容）
    op.add_column(
        "llm_providers",
        sa.Column(
            "provider_type",
            sa.String(50),
            nullable=False,
            server_default="openai",
            comment="提供商类型：openai, anthropic, deepseek, qwen, zhipu, grok",
        ),
    )

    # 添加 max_tokens 字段
    op.add_column(
        "llm_providers",
        sa.Column("max_tokens", sa.Integer(), nullable=True, comment="最大生成 token 数"),
    )

    # 添加 timeout 字段
    op.add_column(
        "llm_providers",
        sa.Column("timeout", sa.Integer(), nullable=False, server_default="60", comment="请求超时时间（秒）"),
    )

    # 添加 extra_params 字段（JSON 格式，用于扩展参数）
    op.add_column(
        "llm_providers",
        sa.Column(
            "extra_params",
            sa.JSON(),
            nullable=False,
            server_default="{}",
            comment="额外参数（JSON 格式）",
        ),
    )

    # 创建索引以加速按 provider_type 查询
    op.create_index(
        "ix_llm_providers_provider_type",
        "llm_providers",
        ["provider_type"],
    )


def downgrade() -> None:
    """回滚迁移。"""
    # 删除索引
    op.drop_index("ix_llm_providers_provider_type", table_name="llm_providers")

    # 删除新增的字段
    op.drop_column("llm_providers", "extra_params")
    op.drop_column("llm_providers", "timeout")
    op.drop_column("llm_providers", "max_tokens")
    op.drop_column("llm_providers", "provider_type")
