"""动态决策系统节点。

新增三个 LangGraph 节点：
- intent_inference_node:  意图推断（读 session_events，写 intent_result）
- goal_evaluation_node:   目标校验与决策（读 intent_result + player_memory，写 goal_evaluation_result）
- memory_update_node:     更新玩家长期记忆（读 goal_evaluation_result，写 player_memory 表）
"""

import json
import logging
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.core.agents.decision_models import (
    BehaviorProfileMemory,
    GoalEvaluationResult,
    InferredIntent,
)
from src.core.agents.decision_prompts import (
    GOAL_EVALUATION_SYSTEM,
    GOAL_EVALUATION_USER,
    INTENT_INFERENCE_SYSTEM,
    INTENT_INFERENCE_USER,
)
from src.core.agents.state import AnalysisState
from src.core.llm.factory import get_llm

logger = logging.getLogger(__name__)

_SINGLE_CALL_TIMEOUT = 60


# ── 节点1：意图推断 ──

async def intent_inference_node(state: AnalysisState) -> dict[str, Any]:
    """推断玩家本次会话意图和下次可能的行为方向。

    读取最近一次会话的 session_events，结合历史意图记录和玩家记忆，
    用 LLM 推断本次意图并预测下次行为。
    """
    import asyncio

    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    player_memory = state.get("player_memory") or {}

    try:
        session_events = await _load_session_events(user_id, tenant_id)
        recent_intents = await _load_recent_intents(user_id, tenant_id, limit=3)

        session_events_text = (
            json.dumps(session_events, ensure_ascii=False, indent=2)
            if session_events
            else "（本次会话无行为事件数据）"
        )
        player_memory_text = (
            json.dumps(player_memory, ensure_ascii=False, indent=2)
            if player_memory
            else "（暂无玩家记忆，首次分析）"
        )
        recent_intents_text = (
            json.dumps(recent_intents, ensure_ascii=False, indent=2)
            if recent_intents
            else "（无历史意图记录）"
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", INTENT_INFERENCE_SYSTEM),
            ("human", INTENT_INFERENCE_USER),
        ])

        llm = await get_llm(model_type="fast")
        llm_structured = llm.with_structured_output(InferredIntent, method="json_mode")
        chain = prompt | llm_structured

        intent: InferredIntent = await asyncio.wait_for(
            chain.ainvoke({
                "user_id": user_id,
                "session_events": session_events_text,
                "player_memory": player_memory_text,
                "recent_intents": recent_intents_text,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )

        logger.info(
            "[intent_inference] 推断完成, user_id=%s, confidence=%s, next_count=%d",
            user_id,
            intent.intent_confidence,
            len(intent.next_likely),
        )
        return {"intent_result": intent.model_dump()}

    except Exception as e:
        logger.error("[intent_inference] 意图推断失败: %s", e)
        return {
            "intent_result": InferredIntent(
                session_summary="意图推断失败，使用空默认值",
                intent_confidence="low",
            ).model_dump(),
            "errors": [f"意图推断失败: {e}"],
        }


# ── 节点2：目标校验与决策 ──

async def goal_evaluation_node(state: AnalysisState) -> dict[str, Any]:
    """校验当前目标进度，做出继续/降级/切换决策。

    有历史目标时：对比完成度和代价偏差，结合玩家记忆决策。
    无历史目标时：基于意图推断生成新目标（decision=new）。
    """
    import asyncio

    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    snapshot = state.get("snapshot", {})
    intent_result = state.get("intent_result") or {}
    player_memory = state.get("player_memory") or {}

    try:
        last_intent = await _load_last_intent(user_id, tenant_id)

        snapshot_text = json.dumps(snapshot, ensure_ascii=False)
        intent_text = json.dumps(intent_result, ensure_ascii=False, indent=2)
        memory_text = (
            json.dumps(player_memory, ensure_ascii=False, indent=2)
            if player_memory
            else "（暂无玩家记忆）"
        )
        last_intent_text = (
            json.dumps(last_intent, ensure_ascii=False, indent=2)
            if last_intent
            else "（无历史目标，首次分析）"
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", GOAL_EVALUATION_SYSTEM),
            ("human", GOAL_EVALUATION_USER),
        ])

        llm = await get_llm(model_type="default")
        llm_structured = llm.with_structured_output(GoalEvaluationResult, method="json_mode")
        chain = prompt | llm_structured

        evaluation: GoalEvaluationResult = await asyncio.wait_for(
            chain.ainvoke({
                "snapshot_text": snapshot_text,
                "intent_result": intent_text,
                "player_memory": memory_text,
                "last_intent_record": last_intent_text,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )

        logger.info(
            "[goal_evaluation] 决策完成, user_id=%s, decision=%s, progress=%s",
            user_id,
            evaluation.decision,
            evaluation.goal_progress,
        )
        return {"goal_evaluation_result": evaluation.model_dump()}

    except Exception as e:
        logger.error("[goal_evaluation] 目标校验失败: %s", e)
        return {
            "goal_evaluation_result": GoalEvaluationResult(
                has_active_goal=False,
                decision="new",
                decision_reason=f"目标校验失败，回退到新目标模式: {e}",
            ).model_dump(),
            "errors": [f"目标校验失败: {e}"],
        }


# ── 节点3：更新玩家长期记忆 ──

async def memory_update_node(state: AnalysisState) -> dict[str, Any]:
    """更新玩家长期记忆。

    两个操作：
    1. upsert player_memory：增量更新行为画像（每次），目标历史（出现≥2次后统计）
    2. insert player_intent：记录本次意图推断和决策结论

    不调用 LLM，纯数据操作。
    """
    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    snapshot = state.get("snapshot", {})
    intent_result = state.get("intent_result") or {}
    goal_eval = state.get("goal_evaluation_result") or {}

    try:
        await _upsert_player_memory(user_id, tenant_id, snapshot, intent_result, goal_eval)
        await _save_player_intent(
            user_id=user_id,
            tenant_id=tenant_id,
            intent_result=intent_result,
            goal_eval=goal_eval,
        )

        logger.info("[memory_update] 完成, user_id=%s", user_id)
        return {}

    except Exception as e:
        logger.error("[memory_update] 记忆更新失败: %s", e)
        return {"errors": [f"记忆更新失败: {e}"]}


# ── 内部辅助函数 ──

async def _load_session_events(user_id: str, tenant_id: str) -> list[dict]:
    """加载最近一次会话的事件序列（按 session_id 最新的一组）。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        latest = await session.execute(
            text("""
                SELECT session_id
                FROM session_events
                WHERE user_id = :user_id AND tenant_id = :tenant_id
                ORDER BY occurred_at DESC
                LIMIT 1
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = latest.first()
        if not row:
            return []

        session_id = row.session_id

        events_result = await session.execute(
            text("""
                SELECT event_type, event_data, occurred_at
                FROM session_events
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND session_id = :session_id
                ORDER BY occurred_at ASC
                LIMIT 100
            """),
            {"user_id": user_id, "tenant_id": tenant_id, "session_id": session_id},
        )
        rows = events_result.fetchall()

    return [
        {
            "event_type": r.event_type,
            "event_data": r.event_data,
            "occurred_at": r.occurred_at.isoformat(),
        }
        for r in rows
    ]


async def _load_recent_intents(user_id: str, tenant_id: str, limit: int = 3) -> list[dict]:
    """加载最近 N 次的意图推断记录。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT inferred_intent, current_goal, goal_status,
                       goal_progress, evaluation_result, created_at
                FROM player_intent
                WHERE user_id = :user_id AND tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"user_id": user_id, "tenant_id": tenant_id, "limit": limit},
        )
        rows = result.fetchall()

    return [
        {
            "inferred_intent": r.inferred_intent,
            "current_goal": r.current_goal,
            "goal_status": r.goal_status,
            "goal_progress": r.goal_progress,
            "evaluation_result": r.evaluation_result,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def _load_last_intent(user_id: str, tenant_id: str) -> dict | None:
    """加载最近一次 active 状态的目标记录。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT current_goal, goal_type, goal_status, goal_progress,
                       cost_expected, cost_actual, evaluation_result, created_at
                FROM player_intent
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND goal_status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.first()

    if not row:
        return None

    return {
        "current_goal": row.current_goal,
        "goal_type": row.goal_type,
        "goal_status": row.goal_status,
        "goal_progress": row.goal_progress,
        "cost_expected": row.cost_expected,
        "cost_actual": row.cost_actual,
        "evaluation_result": row.evaluation_result,
        "created_at": row.created_at.isoformat(),
    }


async def _upsert_player_memory(
    user_id: str,
    tenant_id: str,
    snapshot: dict,
    intent_result: dict,
    goal_eval: dict,
) -> None:
    """Upsert player_memory 记录。

    行为画像每次增量更新（滑动平均）。
    目标历史在 goal_type 有值时累计统计。
    """
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        existing = await session.execute(
            text("""
                SELECT id, behavior_profile, goal_history, analysis_count
                FROM player_memory
                WHERE user_id = :user_id AND tenant_id = :tenant_id
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = existing.first()

        if row is None:
            new_profile = _build_initial_behavior_profile(snapshot)
            new_goal_history: dict = {}

            await session.execute(
                text("""
                    INSERT INTO player_memory (
                        tenant_id, user_id,
                        behavior_profile, goal_history,
                        analysis_count, created_at, updated_at
                    ) VALUES (
                        :tenant_id, :user_id,
                        :behavior_profile, :goal_history,
                        1, now(), now()
                    )
                """),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "behavior_profile": json.dumps(new_profile, ensure_ascii=False),
                    "goal_history": json.dumps(new_goal_history, ensure_ascii=False),
                },
            )
        else:
            existing_profile = row.behavior_profile or {}
            existing_goal_history = row.goal_history or {}
            analysis_count = (row.analysis_count or 0) + 1

            updated_profile = _update_behavior_profile(existing_profile, snapshot, analysis_count)
            updated_goal_history = _update_goal_history(
                existing_goal_history,
                goal_eval,
                analysis_count,
            )

            await session.execute(
                text("""
                    UPDATE player_memory
                    SET behavior_profile = :behavior_profile,
                        goal_history = :goal_history,
                        analysis_count = :analysis_count,
                        updated_at = now()
                    WHERE id = :id
                """),
                {
                    "behavior_profile": json.dumps(updated_profile, ensure_ascii=False),
                    "goal_history": json.dumps(updated_goal_history, ensure_ascii=False),
                    "analysis_count": analysis_count,
                    "id": row.id,
                },
            )


def _build_initial_behavior_profile(snapshot: dict) -> dict:
    """从快照构建初始行为画像。"""
    return BehaviorProfileMemory(
        avg_spend_per_session=float(snapshot.get("gold_spent", 0) or 0),
        avg_session_minutes=float(snapshot.get("session_minutes", 0) or 0),
    ).model_dump()


def _update_behavior_profile(existing: dict, snapshot: dict, count: int) -> dict:
    """滑动平均更新行为画像。count 为更新后的累计次数。"""
    profile = BehaviorProfileMemory(**existing) if existing else BehaviorProfileMemory()

    n = count
    new_spend = float(snapshot.get("gold_spent", 0) or 0)
    new_minutes = float(snapshot.get("session_minutes", 0) or 0)

    profile.avg_spend_per_session = (
        profile.avg_spend_per_session * (n - 1) / n + new_spend / n
        if n > 0 else new_spend
    )
    profile.avg_session_minutes = (
        profile.avg_session_minutes * (n - 1) / n + new_minutes / n
        if n > 0 else new_minutes
    )

    avg = profile.avg_spend_per_session
    if avg > 500:
        profile.spend_tendency = "high"
    elif avg > 100:
        profile.spend_tendency = "medium"
    else:
        profile.spend_tendency = "low"

    return profile.model_dump()


def _update_goal_history(existing: dict, goal_eval: dict, analysis_count: int) -> dict:
    """累计更新目标历史统计。同一 goal_type 出现 >=2 次才写入。"""
    goal_type = goal_eval.get("suggested_goal_type") or goal_eval.get("goal_type")
    if not goal_type:
        return existing

    history = dict(existing)
    entry = history.get(goal_type, {"total": 0, "success": 0, "avg_cost": 0.0, "abandon_reasons": []})

    entry["total"] = entry.get("total", 0) + 1

    decision = goal_eval.get("decision")
    if decision in ("switch", "downgrade"):
        reason = goal_eval.get("decision_reason", "")
        reasons = list(entry.get("abandon_reasons", []))
        reasons.append(reason)
        entry["abandon_reasons"] = reasons[-5:]  # 保留最近5条

    progress = goal_eval.get("goal_progress") or 0.0
    if progress >= 1.0:
        entry["success"] = entry.get("success", 0) + 1

    cost_deviation = goal_eval.get("cost_deviation")
    if cost_deviation is not None:
        n = entry["total"]
        entry["avg_cost"] = (
            entry.get("avg_cost", 0.0) * (n - 1) / n + cost_deviation / n
            if n > 0 else cost_deviation
        )

    # 出现 >=2 次才写入 history
    if entry["total"] >= 2:
        history[goal_type] = entry

    return history


async def _save_player_intent(
    user_id: str,
    tenant_id: str,
    intent_result: dict,
    goal_eval: dict,
) -> None:
    """写入本次意图推断和决策结论到 player_intent 表。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    current_goal = goal_eval.get("suggested_goal") or intent_result.get("session_summary", "")
    goal_type = goal_eval.get("suggested_goal_type")
    decision = goal_eval.get("decision", "new")
    decision_reason = goal_eval.get("decision_reason", "")
    goal_progress = goal_eval.get("goal_progress")
    cost_deviation = goal_eval.get("cost_deviation")

    goal_status_map = {
        "continue": "active",
        "downgrade": "active",
        "switch": "switched",
        "new": "active",
    }
    goal_status = goal_status_map.get(decision, "active")

    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO player_intent (
                    tenant_id, user_id,
                    inferred_intent, current_goal, goal_type,
                    goal_status, goal_progress,
                    cost_actual, evaluation_result, evaluation_reason
                ) VALUES (
                    :tenant_id, :user_id,
                    :inferred_intent, :current_goal, :goal_type,
                    :goal_status, :goal_progress,
                    :cost_actual, :evaluation_result, :evaluation_reason
                )
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "inferred_intent": json.dumps(intent_result, ensure_ascii=False),
                "current_goal": current_goal,
                "goal_type": goal_type,
                "goal_status": goal_status,
                "goal_progress": goal_progress,
                "cost_actual": cost_deviation,
                "evaluation_result": decision,
                "evaluation_reason": decision_reason,
            },
        )
