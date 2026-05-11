"""智能体工具集。

工具通过 create_tools() 工厂创建，闭包注入 tenant_id / user_id，
LLM 只需传业务参数（如 limit），无需感知租户隔离细节。

工具列表：
  基础工具（原有）：
    - query_player_history:   查询历史分析记录，检测行为趋势
    - query_similar_players:  查询相似玩家，对比参考
    - dynamic_rag_query:      动态查询游戏知识库

  监督机制工具（新增）：
    - get_action_tracking:    查询上次推荐行动的完成情况
    - detect_anomaly:         检测突发异常（流失风险、活跃度骤降等）
"""

import json
import logging
from datetime import datetime, timezone

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


async def _get_action_tracking(user_id: str, tenant_id: str, snapshot: dict) -> str:
    """查询上次推荐行动的完成情况，结合当前快照实时计算状态。

    完成判断优先级：
    1. 指标对比（主要）：snapshot[goal_metric] >= goal_value → completed
    2. 截止时间（兜底）：now > deadline 且指标未达成 → timeout
    3. 状态字段（已持久化的 completed/abandoned）直接使用
    """
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT id, action_type, action_desc, goal_metric, goal_value,
                       baseline_value, expected_hours, deadline, status, created_at
                FROM action_tracking
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND status = 'tracking'
                ORDER BY created_at DESC
                LIMIT 10
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        rows = result.fetchall()

    if not rows:
        return "无进行中的行动追踪记录（首次分析或上次行动均已结束）"

    tracking_items = []
    for row in rows:
        item: dict = {
            "action_type": row.action_type,
            "action_desc": row.action_desc or "",
            "created_at": row.created_at.isoformat(),
        }

        # 实时计算完成状态
        computed_status = "tracking"
        progress_desc = ""

        if row.goal_metric and row.goal_value is not None:
            current_val = _extract_metric(snapshot, row.goal_metric)
            if current_val is not None:
                progress = current_val - (row.baseline_value or 0)
                target_delta = row.goal_value - (row.baseline_value or 0)
                pct = (progress / target_delta * 100) if target_delta > 0 else 0
                progress_desc = (
                    f"{row.goal_metric}: {row.baseline_value or '?'} → "
                    f"{current_val} / 目标 {row.goal_value} ({pct:.0f}%)"
                )
                if current_val >= row.goal_value:
                    computed_status = "completed"
            else:
                progress_desc = f"{row.goal_metric}: 快照中未找到该指标"

        # 截止时间兜底
        if computed_status == "tracking" and row.deadline:
            deadline_dt = row.deadline if row.deadline.tzinfo else row.deadline.replace(tzinfo=timezone.utc)
            if now > deadline_dt:
                computed_status = "timeout"
                progress_desc += f"（已超过截止时间 {deadline_dt.isoformat()}）"

        item["status"] = computed_status
        item["progress"] = progress_desc
        tracking_items.append(item)

    # 汇总统计
    completed = sum(1 for t in tracking_items if t["status"] == "completed")
    timeout = sum(1 for t in tracking_items if t["status"] == "timeout")
    in_progress = sum(1 for t in tracking_items if t["status"] == "tracking")

    summary = (
        f"追踪行动共 {len(tracking_items)} 条："
        f"已完成 {completed} / 超时 {timeout} / 进行中 {in_progress}\n\n"
    )
    summary += json.dumps(tracking_items, ensure_ascii=False, indent=2)
    return summary


def _extract_metric(snapshot: dict, metric: str) -> float | None:
    """从快照中提取指标值，支持顶层字段和 stats 嵌套字段。"""
    # 顶层字段
    if metric in snapshot:
        val = snapshot[metric]
        if isinstance(val, (int, float)):
            return float(val)

    # stats 嵌套字段
    stats = snapshot.get("stats", {})
    if isinstance(stats, dict) and metric in stats:
        val = stats[metric]
        if isinstance(val, (int, float)):
            return float(val)

    return None


async def _detect_anomaly(user_id: str, tenant_id: str, snapshot: dict) -> str:
    """检测玩家当前是否存在异常情况。

    检测规则（基于规则，不调用 LLM）：
    1. 活跃度骤降：最近一次分析 engagement_level 高于当前快照推断值
    2. 流失风险：本次在线时长增量 < 历史均值的 30%
    3. 行动全部超时：所有追踪行动均已超时
    4. 重复卡关：当前 bottlenecks 与上次分析完全相同
    """
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    anomalies: list[str] = []
    now = datetime.now(timezone.utc)

    # 查询最近两次分析结果
    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT output_json, analyzed_at
                FROM analysis_results
                WHERE user_id = :user_id AND tenant_id = :tenant_id
                ORDER BY analyzed_at DESC
                LIMIT 2
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        history_rows = result.fetchall()

        # 查询超时行动数量
        timeout_result = await session.execute(
            text("""
                SELECT COUNT(*) as cnt
                FROM action_tracking
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND status = 'tracking'
                  AND deadline IS NOT NULL
                  AND deadline < :now
            """),
            {"user_id": user_id, "tenant_id": tenant_id, "now": now},
        )
        timeout_row = timeout_result.first()
        timeout_count = timeout_row.cnt if timeout_row else 0

    # 规则1：行动全部超时
    if timeout_count > 0:
        # 检查是否还有未超时的追踪行动
        anomalies.append(f"行动超时: {timeout_count} 条追踪行动已超过截止时间未完成")

    if not history_rows:
        if anomalies:
            return "\n".join(anomalies)
        return "无异常"

    last_output = history_rows[0].output_json
    if isinstance(last_output, str):
        last_output = json.loads(last_output)

    last_profile = last_output.get("player_profile", {})

    # 规则2：活跃度骤降（需要快照中有 engagement 相关指标推断）
    last_engagement = last_profile.get("engagement_level", "")
    # 用 play_hours 增量粗略推断当前活跃度
    stats = snapshot.get("stats", {})
    current_play_hours = stats.get("play_hours", 0) if isinstance(stats, dict) else 0

    if len(history_rows) >= 2:
        prev_output = history_rows[1].output_json
        if isinstance(prev_output, str):
            prev_output = json.loads(prev_output)
        prev_snapshot = prev_output.get("snapshot", {})
        prev_stats = prev_snapshot.get("stats", {}) if isinstance(prev_snapshot, dict) else {}
        prev_play_hours = prev_stats.get("play_hours", 0) if isinstance(prev_stats, dict) else 0

        # 本次增量 vs 上次增量
        last_snapshot = last_output.get("snapshot", {})
        last_stats = last_snapshot.get("stats", {}) if isinstance(last_snapshot, dict) else {}
        last_play_hours = last_stats.get("play_hours", 0) if isinstance(last_stats, dict) else 0

        last_delta = last_play_hours - prev_play_hours
        current_delta = current_play_hours - last_play_hours

        if last_delta > 0 and current_delta < last_delta * 0.3:
            anomalies.append(
                f"流失风险: 本次游戏时长增量 {current_delta:.1f}h，"
                f"仅为上次增量 {last_delta:.1f}h 的 {current_delta / last_delta * 100:.0f}%"
            )

    # 规则3：活跃度等级骤降
    if last_engagement in ("high", "medium"):
        # 用 play_hours 增量粗略判断当前活跃度是否骤降
        # 若增量极低（< 1h）且上次是 high，视为骤降
        last_snapshot = last_output.get("snapshot", {})
        last_stats = last_snapshot.get("stats", {}) if isinstance(last_snapshot, dict) else {}
        last_play_hours_val = last_stats.get("play_hours", 0) if isinstance(last_stats, dict) else 0
        delta = current_play_hours - last_play_hours_val
        if last_engagement == "high" and delta < 1:
            anomalies.append(f"活跃度骤降: 上次分析为 {last_engagement}，本次游戏时长增量仅 {delta:.1f}h")

    # 规则4：重复卡关
    last_bottlenecks = set(last_profile.get("bottlenecks", []))
    current_bottlenecks_raw = snapshot.get("bottlenecks", [])
    if isinstance(current_bottlenecks_raw, list):
        current_bottlenecks = set(current_bottlenecks_raw)
        if last_bottlenecks and current_bottlenecks and last_bottlenecks == current_bottlenecks:
            anomalies.append(f"重复卡关: 瓶颈与上次分析完全相同 — {', '.join(last_bottlenecks)}")

    return "\n".join(anomalies) if anomalies else "无异常"


def create_tools(tenant_id: str, user_id: str, snapshot: dict | None = None) -> list:
    """创建绑定了租户和用户上下文的工具实例。

    通过闭包注入 tenant_id / user_id / snapshot，后续添加新工具也在此注册。

    Args:
        tenant_id: 租户 ID
        user_id:   玩家 ID
        snapshot:  当前玩家快照，监督机制工具需要用来计算完成状态和检测异常
    """
    _snapshot = snapshot or {}

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

    @tool
    async def get_action_tracking() -> str:
        """查询该玩家上次推荐行动的完成情况。

        结合当前快照实时计算每个追踪行动的完成状态：
        - completed: 快照指标已达到目标值
        - timeout:   已超过截止时间但指标未达成
        - tracking:  仍在进行中

        返回追踪摘要和每条行动的进度详情。
        无追踪记录时返回"首次分析"提示。
        """
        return await _get_action_tracking(user_id, tenant_id, _snapshot)

    @tool
    async def detect_anomaly() -> str:
        """检测玩家当前是否存在异常情况。

        检测以下异常类型：
        - 流失风险：本次游戏时长增量远低于历史均值
        - 活跃度骤降：上次高活跃，本次几乎不活跃
        - 行动超时：追踪行动超过截止时间未完成
        - 重复卡关：瓶颈与上次分析完全相同

        无异常时返回"无异常"。有异常时返回异常描述列表，
        action_reasoning 节点应优先处理异常情况。
        """
        return await _detect_anomaly(user_id, tenant_id, _snapshot)

    return [
        query_player_history,
        query_similar_players,
        dynamic_rag_query,
        get_action_tracking,
        detect_anomaly,
    ]
