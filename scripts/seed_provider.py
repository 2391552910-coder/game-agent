"""从 .env 配置创建初始 LLM Provider。

用法:
    uv run python scripts/seed_provider.py
"""

import asyncio
import logging
import sys

from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main():
    from src.config import settings
    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        # 检查是否已有 provider
        existing = await session.execute(text("SELECT COUNT(*) FROM llm_providers"))
        count = existing.scalar()

        if count > 0:
            print(f"已有 {count} 个 provider，跳过 seed")
            return

        # 插入 default model provider
        await session.execute(
            text("""
                INSERT INTO llm_providers (name, provider, model, api_key, base_url, weight, model_type)
                VALUES (:name, :provider, :model, :api_key, :base_url, :weight, :model_type)
            """),
            {
                "name": f"{settings.llm_provider}-default",
                "provider": settings.llm_provider,
                "model": settings.openai_default_model,
                "api_key": settings.openai_api_key,
                "base_url": settings.openai_base_url,
                "weight": 1,
                "model_type": "default",
            },
        )
        print(f"已创建 default provider: {settings.llm_provider}/{settings.openai_default_model}")

        # 如果 fast model 与 default 不同，额外插入
        if settings.openai_fast_model != settings.openai_default_model:
            await session.execute(
                text("""
                    INSERT INTO llm_providers (name, provider, model, api_key, base_url, weight, model_type)
                    VALUES (:name, :provider, :model, :api_key, :base_url, :weight, :model_type)
                """),
                {
                    "name": f"{settings.llm_provider}-fast",
                    "provider": settings.llm_provider,
                    "model": settings.openai_fast_model,
                    "api_key": settings.openai_api_key,
                    "base_url": settings.openai_base_url,
                    "weight": 1,
                    "model_type": "fast",
                },
            )
            print(f"已创建 fast provider: {settings.llm_provider}/{settings.openai_fast_model}")

    print("seed 完成")


if __name__ == "__main__":
    asyncio.run(main())
