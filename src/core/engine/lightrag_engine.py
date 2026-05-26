"""LightRAG 引擎封装层。

存储架构：
- KV 存储：Redis（缓存 + 文档状态）
- 图存储：Neo4j（知识图谱）
- 向量存储：Milvus（语义检索）
- 业务数据：PostgreSQL（租户/配额/分析结果，不走 LightRAG）

LLM 集成：使用统一 LLM 工厂，支持多提供商。
"""

import logging
import os
from functools import partial
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from lightrag import LightRAG
from lightrag.rerank import ali_rerank
from lightrag.utils import EmbeddingFunc

from src.config import settings
from src.core.engine.embedding import embed_texts

logger = logging.getLogger(__name__)

# 工作目录

WORKING_DIR = settings.rag_working_dir
os.makedirs(WORKING_DIR, exist_ok=True)


# 存储后端环境变量注入

# ── Redis（KV 缓存 + 文档状态） ──
os.environ["REDIS_URI"] = settings.redis_url
# 增加 Redis 超时，Windows 异步连接池需要更长时间
os.environ.setdefault("REDIS_SOCKET_TIMEOUT", "60.0")
os.environ.setdefault("REDIS_CONNECT_TIMEOUT", "30.0")
os.environ.setdefault("REDIS_MAX_CONNECTIONS", "50")

# ── Neo4j（图存储） ──
neo4j_uri = settings.neo4j_uri
if neo4j_uri.startswith("bolt://"):
    neo4j_uri = neo4j_uri.replace("bolt://", "neo4j://", 1)

os.environ.setdefault("NEO4J_URI", neo4j_uri)
os.environ.setdefault("NEO4J_USERNAME", settings.neo4j_username)
os.environ.setdefault("NEO4J_PASSWORD", settings.neo4j_password)
os.environ.setdefault("NEO4J_DATABASE", settings.neo4j_database)


# LLM 函数（使用统一 LLM 工厂）


async def llm_model_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict[str, str]] | None = None,
    keyword_extraction: bool = False,
    **kwargs: Any,
) -> str:
    """调用 LLM 完成文本生成（用于 LightRAG）。"""
    from langchain_openai import ChatOpenAI

    # 构建 LangChain 消息列表
    messages: list[HumanMessage | SystemMessage | AIMessage] = []

    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))

    # 添加历史消息（如果有）
    if history_messages:
        for msg in history_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))

    # 添加当前提示
    messages.append(HumanMessage(content=prompt))

    # 直接创建 LLM 实例（避免缓存问题）
    llm = ChatOpenAI(
        model=settings.openai_default_model,
        temperature=0.1,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        streaming=False,
        max_retries=2,
    )
    response = await llm.ainvoke(messages)

    # 返回文本内容
    return response.content


# Embedding 函数（Qwen text-embedding-v4）

embedding_func = EmbeddingFunc(
    embedding_dim=settings.embedding_dim,
    max_token_size=8192,
    func=lambda texts: embed_texts(
        texts,
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        embedding_dim=settings.embedding_dim,
    ),
)


# Rerank 函数（Qwen gte-rerank-v2）


def _build_rerank_model_func():
    if not settings.rerank_enabled:
        return None

    return partial(
        ali_rerank,
        api_key=settings.rerank_api_key,
        model=settings.rerank_model,
    )


# 初始化


async def warmup_connections() -> None:
    """预热所有连接池，避免首次查询时的初始化开销。

    在应用启动时调用此函数，预先建立：
    - Redis 连接池
    - Milvus 向量数据库连接
    - Neo4j 图数据库连接
    - LLM 服务连接
    - Embedding 服务连接

    这样可以将首次查询的初始化开销转移到应用启动阶段。
    """
    import time

    start = time.time()
    print("[WARMUP] Starting connection pool warmup...")

    # 1. 预热 Redis 连接
    print("[WARMUP] Step 1/5: Warming up Redis connections...")
    redis_start = time.time()
    from redis import asyncio as aioredis
    redis_conn = aioredis.from_url(settings.redis_url)
    try:
        await redis_conn.ping()
        await redis_conn.set("_warmup_test", "ok")
        await redis_conn.get("_warmup_test")
        await redis_conn.delete("_warmup_test")
        print(f"[WARMUP] Redis warmup completed in {time.time() - redis_start:.2f}s")
    finally:
        await redis_conn.close()

    # 2. 预热 Milvus 连接
    print("[WARMUP] Step 2/5: Warming up Milvus connections...")
    milvus_start = time.time()
    from pymilvus import MilvusClient
    milvus_client = MilvusClient(uri=settings.milvus_uri)
    try:
        milvus_client.has_collection("chunks")
        print(f"[WARMUP] Milvus warmup completed in {time.time() - milvus_start:.2f}s")
    finally:
        milvus_client.close()

    # 3. 预热 Neo4j 连接
    print("[WARMUP] Step 3/5: Warming up Neo4j connections...")
    neo4j_start = time.time()
    from neo4j import AsyncGraphDatabase
    neo4j_driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri.replace("bolt://", "neo4j://", 1),
        auth=(settings.neo4j_username, settings.neo4j_password)
    )
    try:
        async with neo4j_driver.session() as session:
            await session.run("RETURN 1")
        print(f"[WARMUP] Neo4j warmup completed in {time.time() - neo4j_start:.2f}s")
    finally:
        await neo4j_driver.close()

    # 4. 预热 Embedding 服务
    print("[WARMUP] Step 4/5: Warming up Embedding service...")
    embedding_start = time.time()
    try:
        # 使用我们已经定义的 embedding_func
        await embedding_func(["warmup test"])
        print(f"[WARMUP] Embedding warmup completed in {time.time() - embedding_start:.2f}s")
    except Exception as e:
        print(f"[WARMUP] Embedding warmup failed (may be expected): {e}")

    # 5. 预热 LLM 服务
    print("[WARMUP] Step 5/5: Warming up LLM service...")
    llm_start = time.time()
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model=settings.openai_default_model,
        temperature=0.1,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        streaming=False,
        max_retries=1,
    )
    try:
        await llm.ainvoke("Hello, this is a warmup request.")
        print(f"[WARMUP] LLM warmup completed in {time.time() - llm_start:.2f}s")
    except Exception as e:
        print(f"[WARMUP] LLM warmup failed (may be expected): {e}")

    total_time = time.time() - start
    print(f"[WARMUP] All connections warmed up in {total_time:.2f}s")


async def initialize_rag(workspace: str | None = None) -> LightRAG:
    """创建并初始化 LightRAG 实例。

    Args:
        workspace: 多租户隔离标识（如租户 ID 或游戏名）。
                   Milvus 通过 collection 前缀隔离，Neo4j 通过 Label 隔离。

    Returns:
        已初始化的 LightRAG 实例。
    """
    rag = LightRAG(
        working_dir=WORKING_DIR,
        workspace=workspace,
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        rerank_model_func=_build_rerank_model_func(),
        llm_model_max_async=settings.lightrag_llm_max_async,
        summary_max_tokens=10000,
        chunk_token_size=settings.lightrag_chunk_token_size,
        chunk_overlap_token_size=settings.lightrag_chunk_overlap_token_size,
        kv_storage="RedisKVStorage",
        graph_storage="Neo4JStorage",
        vector_storage="MilvusVectorDBStorage",
        doc_status_storage="RedisDocStatusStorage",
        vector_db_storage_cls_kwargs={
            "milvus_uri": settings.milvus_uri,
            "milvus_db_name": settings.milvus_db_name,
            "milvus_user": settings.milvus_user,
            "milvus_password": settings.milvus_password,
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "hnsw_m": 24,
            "hnsw_ef_construction": 360,
            "hnsw_ef": 200,
            "cosine_better_than_threshold": 0.6,
        },
    )

    await rag.initialize_storages()
    return rag


# 全局单例

_rag: LightRAG | None = None


async def get_rag(workspace: str | None = None) -> LightRAG:
    """获取全局 LightRAG 单例。

    首次调用时创建并初始化实例，后续调用返回同一实例。

    用法：
        rag = await get_rag()
        await rag.ainsert("文档内容")
        result = await rag.aquery("问题", param=QueryParam(mode="hybrid"))
    """
    global _rag
    if _rag is None:
        started = perf_counter()
        logger.info("[lightrag_timing] initialization status=started workspace=%s", workspace or "default")
        _rag = await initialize_rag(workspace=workspace)
        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "[lightrag_timing] initialization status=completed elapsed_ms=%.2f workspace=%s",
            elapsed_ms,
            workspace or "default",
        )
    return _rag


async def shutdown_rag() -> None:
    """关闭 LightRAG 实例，释放资源。

    应用关闭时调用（如 FastAPI shutdown event）。
    """
    global _rag
    if _rag is not None:
        await _rag.finalize_storages()
        _rag = None
