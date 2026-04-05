"""智能体工具集。

工具通过 create_tools() 工厂创建，闭包注入 tenant_id / user_id，
LLM 只需传业务参数（如 limit），无需感知租户隔离细节。
"""

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


async def _query_player_history(user_id: str, tenant_id: str, limit: int = 5) -> str:
    """查询玩家历史分析记录的核心实现。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT output_json, analyzed_at
                FROM analysis_results
                WHERE user_id = :user_id AND tenant_id = :tenant_id
                ORDER BY analyzed_at DESC
                LIMIT :limit
            """),
            {"user_id": user_id, "tenant_id": tenant_id, "limit": limit},
        )
        rows = result.fetchall()

    if not rows:
        return "该玩家暂无历史分析记录"

    history = []
    for row in rows:
        output = row.output_json
        if isinstance(output, str):
            output = json.loads(output)
        history.append({
            "analyzed_at": row.analyzed_at.isoformat(),
            "playstyle": output.get("player_profile", {}).get("playstyle", "unknown"),
            "engagement_level": output.get("player_profile", {}).get("engagement_level", "unknown"),
            "current_goal": output.get("player_profile", {}).get("current_goal", ""),
            "bottlenecks": output.get("player_profile", {}).get("bottlenecks", []),
            "recommended_action_count": len(output.get("recommended_actions", [])),
        })

    return json.dumps(history, ensure_ascii=False, indent=2)


async def _query_similar_players(
    tenant_id: str,
    current_user_id: str,
    playstyle: str | None = None,
    limit: int = 3,
) -> str:
    """查询相似玩家的核心实现。

    使用 DISTINCT ON 获取每个玩家的最新一条分析，
    可按 playstyle 筛选（PostgreSQL JSON 操作符）。
    """
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    base_sql = """
        SELECT DISTINCT ON (user_id) user_id, output_json, analyzed_at
        FROM analysis_results
        WHERE tenant_id = :tenant_id
          AND user_id != :user_id
    """
    params: dict = {
        "tenant_id": tenant_id,
        "user_id": current_user_id,
        "limit": limit,
    }

    if playstyle:
        base_sql += "\n  AND output_json->'player_profile'->>'playstyle' = :playstyle"
        params["playstyle"] = playstyle

    base_sql += "\nORDER BY user_id, analyzed_at DESC\nLIMIT :limit"

    async with get_session() as session:
        result = await session.execute(text(base_sql), params)
        rows = result.fetchall()

    if not rows:
        return "未找到相似玩家"

    similar = []
    for row in rows:
        output = row.output_json
        if isinstance(output, str):
            output = json.loads(output)
        similar.append({
            "user_id": row.user_id,
            "analyzed_at": row.analyzed_at.isoformat(),
            "playstyle": output.get("player_profile", {}).get("playstyle", "unknown"),
            "engagement_level": output.get("player_profile", {}).get("engagement_level", "unknown"),
            "current_goal": output.get("player_profile", {}).get("current_goal", ""),
            "bottlenecks": output.get("player_profile", {}).get("bottlenecks", []),
            "top_actions": [
                {
                    "action_type": a.get("action_type"),
                    "target": a.get("target"),
                    "priority": a.get("priority"),
                }
                for a in output.get("recommended_actions", [])[:3]
            ],
        })

    return json.dumps(similar, ensure_ascii=False, indent=2)


async def _dynamic_rag_query(query: str) -> str:
    """动态 RAG 检索的核心实现。"""
    from lightrag import QueryParam

    from src.core.engine.lightrag_engine import get_rag

    rag = await get_rag()
    context = await rag.aquery(query, param=QueryParam(mode="hybrid"))
    return context if context else "未找到相关内容"


def create_tools(tenant_id: str, user_id: str) -> list:
    """创建绑定了租户和用户上下文的工具实例。

    通过闭包注入 tenant_id / user_id，后续添加新工具也在此注册。
    """

    @tool
    async def query_player_history(limit: int = 5) -> str:
        """查询当前玩家的历史分析记录，用于检测行为趋势变化。

        返回最近N次分析的摘要，包括玩法风格、活跃度、目标和瓶颈的历史变化。
        适用于判断玩家行为是持续、上升还是下降趋势。

        Args:
            limit: 返回最近N条记录，默认5条
        """
        return await _query_player_history(user_id, tenant_id, limit)

    @tool
    async def query_similar_players(playstyle: str | None = None, limit: int = 3) -> str:
        """查询具有相似特征的玩家及其推荐行动，用于对比参考。

        返回同租户下其他玩家的最近分析结果，包括行为画像和推荐行动。
        可按玩法风格筛选，不传则返回多样化样本。

        Args:
            playstyle: 玩法风格筛选，如"competitive"、"explorer"、"social"。
                       根据快照中的玩家特征推测。不传则不筛选。
            limit: 返回最多N个玩家，默认3
        """
        return await _query_similar_players(tenant_id, user_id, playstyle, limit)

    @tool
    async def dynamic_rag_query(query: str) -> str:
        """根据特定主题查询游戏知识库，获取初始检索未覆盖的详细规则。

        适用于需要深入了解特定游戏机制的场景。

        Args:
            query: 查询主题，应具体明确，如"PVP匹配机制和段位规则"
        """
        return await _dynamic_rag_query(query)

    return [query_player_history, query_similar_players, dynamic_rag_query]
