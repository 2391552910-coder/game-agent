"""LLM Provider 加权轮询负载均衡器。

全局共享 provider 池，按 model_type 分组选择。
健康追踪通过 Redis 连续失败计数实现，超过阈值自动跳过。
"""

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from src.core.llm.models import LLMProviderConfig

logger = logging.getLogger(__name__)

# 健康检查阈值: 连续失败 N 次后标记为不健康
_UNHEALTHY_THRESHOLD = 5
# 健康检查 Redis key 前缀
_HEALTH_KEY_PREFIX = "llm:health:"
# provider 缓存 TTL（秒）
_CACHE_TTL = 300


class NoProviderAvailable(Exception):
    """没有可用的 LLM Provider。"""


class LoadBalancer:
    """LLM Provider 加权轮询负载均衡器。"""

    def __init__(self) -> None:
        # model_type -> provider 列表（带缓存）
        self._pool_cache: dict[str, list[LLMProviderConfig]] = {}
        self._pool_loaded_at: dict[str, float] = {}
        # model_type -> 加权轮询展开后的 provider id 序列
        self._rr_sequences: dict[str, list[str]] = {}
        # model_type -> 当前轮询索引
        self._rr_counters: dict[str, int] = {}
        # provider_id + temperature -> BaseChatModel 实例缓存
        self._llm_cache: dict[str, BaseChatModel] = {}

    async def initialize(self) -> None:
        """预加载所有 model_type 的 provider 缓存。"""
        for mt in ("default", "fast"):
            try:
                await self._load_providers(mt)
            except Exception as e:
                logger.warning("[balancer] 预加载 model_type=%s 失败: %s", mt, e)

    async def select(self, model_type: str = "default", temperature: float = 0.1) -> BaseChatModel:
        """选择一个健康的 provider 并返回 LangChain Chat Model 实例。

        Args:
            model_type: "default" 或 "fast"
            temperature: 生成温度

        Returns:
            LangChain BaseChatModel 实例

        Raises:
            NoProviderAvailable: 没有任何可用的 provider。
        """
        providers = await self._load_providers(model_type)
        if not providers:
            raise NoProviderAvailable(f"无可用 provider, model_type={model_type}")

        # 过滤健康的 provider
        healthy = []
        for p in providers:
            if await self._check_health(p.id):
                healthy.append(p)

        if not healthy:
            logger.warning("[balancer] 所有 provider 不健康, 降级使用全部, model_type=%s", model_type)
            healthy = providers

        # 加权轮询选择
        selected = self._weighted_select(model_type, healthy)
        logger.debug("[balancer] 选中 provider: %s (%s)", selected.name, selected.id[:8])

        # 创建或复用 ChatOpenAI 实例
        return self._get_or_create_llm(selected, temperature)

    async def report_failure(self, provider_id: str) -> None:
        """报告 provider 调用失败，Redis 递增失败计数。"""
        try:
            from src.core.infrastructure.redis import get_redis

            redis = await get_redis()
            key = f"{_HEALTH_KEY_PREFIX}{provider_id}"
            count = await redis.incr(key)
            await redis.expire(key, 3600)  # 1 小时自动过期
            if count >= _UNHEALTHY_THRESHOLD:
                logger.warning("[balancer] provider %s 连续失败 %d 次，标记为不健康", provider_id[:8], count)
        except Exception as e:
            logger.error("[balancer] 报告失败状态异常: %s", e)

    async def report_success(self, provider_id: str) -> None:
        """报告调用成功，Redis 重置失败计数。"""
        try:
            from src.core.infrastructure.redis import get_redis

            redis = await get_redis()
            key = f"{_HEALTH_KEY_PREFIX}{provider_id}"
            await redis.delete(key)
        except Exception as e:
            logger.error("[balancer] 报告成功状态异常: %s", e)

    def invalidate_cache(self) -> None:
        """强制刷新 provider 缓存（CRUD 操作后调用）。"""
        self._pool_cache.clear()
        self._pool_loaded_at.clear()
        self._rr_sequences.clear()
        self._rr_counters.clear()
        logger.info("[balancer] provider 缓存已清空")

    # ── 内部方法 ──

    async def _load_providers(self, model_type: str) -> list[LLMProviderConfig]:
        """从 DB 加载活跃 provider，带 TTL 缓存。"""
        now = time.monotonic()
        loaded_at = self._pool_loaded_at.get(model_type, 0)

        if model_type in self._pool_cache and (now - loaded_at) < _CACHE_TTL:
            return self._pool_cache[model_type]

        from sqlalchemy import text

        from src.core.infrastructure.db import get_session

        async with get_session() as session:
            result = await session.execute(
                text("""
                    SELECT id, name, provider, model, api_key, base_url, weight, model_type
                    FROM llm_providers
                    WHERE is_active = TRUE AND model_type = :model_type AND weight > 0
                    ORDER BY weight DESC
                """),
                {"model_type": model_type},
            )
            rows = result.fetchall()

        providers = [
            LLMProviderConfig(
                id=str(row.id),
                name=row.name,
                provider=row.provider,
                model=row.model,
                api_key=row.api_key,
                base_url=row.base_url,
                weight=row.weight,
                model_type=row.model_type,
            )
            for row in rows
        ]

        self._pool_cache[model_type] = providers
        self._pool_loaded_at[model_type] = now
        # 重建轮询序列
        self._build_rr_sequence(model_type, providers)

        logger.debug("[balancer] 加载 provider: model_type=%s, count=%d", model_type, len(providers))
        return providers

    def _build_rr_sequence(self, model_type: str, providers: list[LLMProviderConfig]) -> None:
        """构建加权轮询展开序列。

        例如 weight=[3,2,1] → 序列 [id_A, id_A, id_A, id_B, id_B, id_C]
        """
        sequence: list[str] = []
        for p in providers:
            sequence.extend([p.id] * p.weight)
        self._rr_sequences[model_type] = sequence
        # 如果当前索引超出范围，重置
        if model_type not in self._rr_counters or self._rr_counters[model_type] >= len(sequence):
            self._rr_counters[model_type] = 0

    def _weighted_select(self, model_type: str, providers: list[LLMProviderConfig]) -> LLMProviderConfig:
        """加权轮询选择一个 provider。"""
        # 过滤后的健康 provider 集合
        healthy_ids = {p.id for p in providers}
        sequence = self._rr_sequences.get(model_type, [])

        if not sequence:
            # 没有预构建序列（可能 provider 列表为空），直接取第一个
            return providers[0]

        # 过滤序列中不健康的 provider
        filtered = [pid for pid in sequence if pid in healthy_ids]
        if not filtered:
            # 全部被过滤，使用原始序列
            filtered = sequence

        idx = self._rr_counters.get(model_type, 0) % len(filtered)
        selected_id = filtered[idx]
        self._rr_counters[model_type] = idx + 1

        # 从 providers 列表中找到对应配置
        provider_map = {p.id: p for p in providers}
        if selected_id in provider_map:
            return provider_map[selected_id]

        # 回退: 取第一个
        return providers[0]

    async def _check_health(self, provider_id: str) -> bool:
        """检查 provider 健康状态。"""
        try:
            from src.core.infrastructure.redis import get_redis

            redis = await get_redis()
            key = f"{_HEALTH_KEY_PREFIX}{provider_id}"
            count = await redis.get(key)
            if count is None:
                return True
            return int(count) < _UNHEALTHY_THRESHOLD
        except Exception:
            # Redis 不可用时默认健康
            return True

    def _get_or_create_llm(self, config: LLMProviderConfig, temperature: float) -> BaseChatModel:
        """创建或复用 LangChain Chat Model 实例。"""
        from src.core.llm.factory import create_llm_from_config

        cache_key = f"{config.id}_{temperature}"
        if cache_key in self._llm_cache:
            return self._llm_cache[cache_key]

        llm = create_llm_from_config(config, temperature)
        self._llm_cache[cache_key] = llm
        return llm


# 全局单例
balancer = LoadBalancer()
