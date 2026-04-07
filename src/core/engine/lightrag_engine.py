"""LightRAG 引擎封装层。

存储架构：
- KV 存储：Redis（缓存 + 文档状态）
- 图存储：Neo4j（知识图谱）
- 向量存储：Milvus（语义检索）
- 业务数据：PostgreSQL（租户/配额/分析结果，不走 LightRAG）

LLM 集成：使用统一 LLM 工厂，支持多提供商。
"""

import os
from functools import partial
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from lightrag import LightRAG
from lightrag.llm.openai import openai_embed
from lightrag.rerank import ali_rerank
from lightrag.utils import EmbeddingFunc

from src.config import settings

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
    """调用统一 LLM 工厂完成文本生成。

    支持 OpenAI、Anthropic、DeepSeek 等多种提供商，
    通过配置动态切换。
    """
    from src.core.llm.factory import get_llm

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

    # 调用统一 LLM 工厂
    llm = await get_llm(model_type="default")
    response = await llm.ainvoke(messages, **kwargs)

    # 返回文本内容
    return response.content


# Embedding 函数（Qwen text-embedding-v4）

embedding_func = EmbeddingFunc(
    embedding_dim=settings.embedding_dim,
    max_token_size=8192,
    func=lambda texts: openai_embed.func(
        texts,
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
    ),
)


# Rerank 函数（Qwen gte-rerank-v2）

rerank_model_func = partial(
    ali_rerank,
    api_key=settings.rerank_api_key,
    model=settings.rerank_model,
)


# 初始化


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
        rerank_model_func=rerank_model_func,
        summary_max_tokens=10000,
        chunk_token_size=512,
        chunk_overlap_token_size=256,
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
        _rag = await initialize_rag(workspace=workspace)
    return _rag


async def shutdown_rag() -> None:
    """关闭 LightRAG 实例，释放资源。

    应用关闭时调用（如 FastAPI shutdown event）。
    """
    global _rag
    if _rag is not None:
        await _rag.finalize_storages()
        _rag = None
