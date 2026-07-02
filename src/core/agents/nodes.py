"""LangGraph 节点函数。

图结构:
START
    → fetch_snapshot          获取玩家快照
    → retrieve_rag_context    主图统一 RAG 检索（一次检索，两节点共享）
    → gather_context          工具收集额外上下文（历史趋势、行动追踪、异常检测）
    → behavior_analysis       行为分析（读 rag_context + enriched_context）
    → action_reasoning        行动推理（读 rag_context + enriched_context + tracking_summary + anomalies）
    → merge_output            组装最终结构化输出
    → tracking_update         更新行动追踪记录（监督机制）
END
"""

import json
import logging
from time import perf_counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate

from src.core.agents.models import ActionList, BehaviorProfile, PlayerAnalysisOutput, RecommendedAction
from src.core.agents.prompts import (
    ACTION_REASONING_SYSTEM,
    ACTION_REASONING_USER,
    BEHAVIOR_ANALYSIS_SYSTEM,
    BEHAVIOR_ANALYSIS_USER,
    CONTEXT_GATHERING_SYSTEM,
)
from src.core.agents.state import AnalysisState
from src.core.agents.tools import create_tools
from src.core.engine.lightrag_engine import get_rag
from src.core.engine.rag_exact_match import retrieve_exact_rag_context
from src.core.llm.factory import get_llm

# ── 防循环/防卡死常量 ──
# gather_context: 最多 N 轮 LLM 对话（每轮可含多个 tool_call）
_MAX_GATHER_ITERATIONS = 3
# gather_context: 总工具调用次数上限（跨所有轮次累计）
_MAX_TOTAL_TOOL_CALLS = 8
# 单次外部调用超时（秒）: RAG 检索、LLM 调用、工具执行
_SINGLE_CALL_TIMEOUT = 60

logger = logging.getLogger(__name__)


# 节点1 获取玩家快照
async def fetch_snapshot_node(state: AnalysisState) -> dict[str, Any]:
    """
    从游戏数据库获取玩家快照数据。
    snapshot 已在上游（Prefect Flow）获取并注入 State，
    此节点负责验证数据完整性。
    """
    snapshot = state.get("snapshot")
    if not snapshot:
        return {"errors": ["snapshot为空，无法分析"]}

    user_id = state["user_id"]
    logger.info("[fetch_snapshot] 快照数据验证通过，user_id=%s", user_id)

    return {}


# 节点2 统一RAG检索
async def retrieve_rag_context_node(state: AnalysisState) -> dict[str, Any]:
    """
    主图统一做一次 RAG 检索，结果注入 rag_context。

    从 snapshot 中提取关键属性构建领域无关的语义查询，
    不包含任何业务特定的关键词（如"玩家"、"游戏"等）。
    """
    snapshot = state.get("snapshot", {})
    query = _build_rag_query(snapshot)
    exact_context = ""

    try:
        import asyncio

        from lightrag import QueryParam

        from src.config import settings

        rag_start = perf_counter()
        rag = await get_rag()
        rag_get_elapsed_ms = (perf_counter() - rag_start) * 1000

        if settings.rag_exact_match_enabled:
            exact_context = await retrieve_exact_rag_context(query)

        query_start = perf_counter()
        context = await asyncio.wait_for(
            rag.aquery(
                query,
                param=QueryParam(
                    mode="hybrid",
                    enable_rerank=settings.rerank_enabled,
                    chunk_top_k=settings.lightrag_chunk_top_k,
                ),
            ),
            timeout=_SINGLE_CALL_TIMEOUT,
        )
        if exact_context:
            context = f"{exact_context}\n\n{context}" if context else exact_context
        query_elapsed_ms = (perf_counter() - query_start) * 1000
        logger.info(
            (
                "[retrieve_rag_context] 检索完成, "
                "lightrag_get_elapsed_ms=%.2f, lightrag_query_elapsed_ms=%.2f, "
                "context_length=%d, query=%s"
            ),
            rag_get_elapsed_ms,
            query_elapsed_ms,
            len(context),
            query[:80],
        )
        return {"rag_context": context}
    except Exception as e:
        logger.error("[retrieve_rag_context] RAG 检索失败: %s", e)
        if exact_context:
            return {
                "rag_context": exact_context,
                "errors": [f"RAG 检索失败，已返回精确匹配上下文: {e}"],
            }
        return {
            "rag_context": "",
            "errors": [f"RAG 检索失败: {e}"],
        }


def _build_rag_query(snapshot: dict) -> str:
    """从快照构建领域无关的 RAG 查询（尽力而为）。

    核心原则：只提取快照中的文本值（名字、类别、描述、活动列表等），
    不使用键名或数字。这些文本值来自业务系统本身，与知识库使用
    相同的领域术语和语言，因此最有可能语义匹配。

    例如：快照 {"current_area": "商业区", "profession": "程序员"}
    → 查询 "商业区 程序员" 能匹配知识库中的商业区规则。
    """
    if not snapshot or not isinstance(snapshot, dict):
        return ""

    text_parts: list[str] = []
    for key, value in snapshot.items():
        # 跳过 ID 字段
        if key.endswith("_id") or key == "id":
            continue
        # 收集文本值
        if isinstance(value, str) and len(value) > 1 and not value.startswith("player_"):
            text_parts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                if isinstance(v, str) and len(v) > 1:
                    text_parts.append(v)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and len(item) > 1:
                    text_parts.append(item)

    return " ".join(text_parts) if text_parts else ""


# 节点3 工具收集额外上下文
async def gather_context_node(state: AnalysisState) -> dict[str, Any]:
    """使用工具收集额外上下文（历史趋势等）。

    内部循环：LLM 决定调用哪些工具 -> 执行 -> LLM 评估是否足够 -> 最多3轮
    使用快速模型，因为这是信息收集阶段，不需要深度推理。
    """
    snapshot = state.get("snapshot", {})
    snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)
    rag_context = state.get("rag_context", "") or "（无额外规则上下文）"

    from src.config import settings

    tools = create_tools(state["tenant_id"], state["user_id"], state.get("snapshot"))
    if not settings.gather_context_enable_dynamic_rag:
        tools = [tool for tool in tools if tool.name != "dynamic_rag_query"]

    llm = await get_llm(model_type="fast")
    llm_with_tools = llm.bind_tools(tools)

    system_prompt = CONTEXT_GATHERING_SYSTEM
    if not settings.gather_context_enable_dynamic_rag:
        system_prompt = (
            f"{system_prompt}\n\n"
            "当前运行模式已禁用 dynamic_rag_query。不要调用该工具；"
            "优先使用已有 RAG 上下文以及历史、追踪、异常检测类工具。"
        )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"玩家快照:\n{snapshot_text}\n\n已有RAG上下文:\n{rag_context}"),
    ]

    enriched_parts: list[str] = []
    tracking_summary: str = ""
    anomalies_found: list[str] = []
    abandoned_ids_found: list[str] = []
    total_tool_calls = 0

    try:
        import asyncio

        iteration_count = 0
        for i in range(_MAX_GATHER_ITERATIONS):
            if total_tool_calls >= _MAX_TOTAL_TOOL_CALLS:
                logger.warning("[gather_context] 达到总工具调用上限 %d，提前结束", _MAX_TOTAL_TOOL_CALLS)
                break

            iteration_count = i + 1
            response = await asyncio.wait_for(
                llm_with_tools.ainvoke(messages),
                timeout=_SINGLE_CALL_TIMEOUT,
            )

            if not response.tool_calls:
                break

            messages.append(response)

            for tool_call in response.tool_calls:
                total_tool_calls += 1
                if total_tool_calls > _MAX_TOTAL_TOOL_CALLS:
                    logger.warning("[gather_context] 工具调用达上限，跳过剩余 tool_call")
                    break

                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                tool_fn = next((t for t in tools if t.name == tool_name), None)
                if tool_fn is None:
                    result = f"未知工具: {tool_name}"
                else:
                    try:
                        result = await asyncio.wait_for(
                            tool_fn.ainvoke(tool_args),
                            timeout=_SINGLE_CALL_TIMEOUT,
                        )
                    except TimeoutError:
                        logger.warning("[gather_context] 工具 %s 执行超时 (%ds)", tool_name, _SINGLE_CALL_TIMEOUT)
                        result = "工具执行超时"
                    except Exception as e:
                        logger.error("[gather_context] 工具 %s 执行失败: %s", tool_name, e)
                        result = f"工具执行失败: {e}"

                enriched_parts.append(f"[{tool_name}] {result}")
                messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))

                # 监督机制工具结果同步写入 state 专用字段，供 action_reasoning 直接读取
                if tool_name == "get_action_tracking":
                    tracking_summary = str(result)
                    # 提取 LLM 判断的冲突 ID（附加在返回文本末尾）
                    result_str = str(result)
                    marker = "\n\nCONFLICT_IDS:"
                    if marker in result_str:
                        try:
                            conflict_json = result_str[result_str.index(marker) + len(marker):]
                            parsed = json.loads(conflict_json)
                            if isinstance(parsed, list):
                                abandoned_ids_found.extend(parsed)
                            # 从 tracking_summary 中去掉 CONFLICT_IDS 行，保持可读性
                            tracking_summary = result_str[:result_str.index(marker)]
                        except Exception:
                            pass
                elif tool_name == "detect_anomaly" and str(result) != "无异常":
                    anomaly_lines = [line for line in str(result).splitlines() if line.strip()]
                    if anomaly_lines:
                        anomalies_found.extend(anomaly_lines)

        enriched_context = "\n\n".join(enriched_parts) if enriched_parts else ""
        logger.info("[gather_context] 上下文收集完成, 轮次=%d", iteration_count)
        result: dict[str, Any] = {"enriched_context": enriched_context}
        if tracking_summary:
            result["tracking_summary"] = tracking_summary
        if anomalies_found:
            result["anomalies"] = anomalies_found
        if abandoned_ids_found:
            result["abandoned_tracking_ids"] = abandoned_ids_found
        return result

    except Exception as e:
        logger.error("[gather_context] 上下文收集失败: %s", e)
        return {
            "enriched_context": "",
            "errors": [f"上下文收集失败: {e}"],
        }


# 节点4 行为分析
async def behavior_analysis_node(state: AnalysisState) -> dict[str, Any]:
    """
    使用快速模型分析玩家行为，输出 BehaviorProfile 结构化模型。
    使用 with_structured_output 确保 LLM 返回合法的 Pydantic 对象，
    而非需要 json.loads 的裸字符串。
    """
    snapshot = state.get("snapshot", {})
    snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)
    rag_context = state.get("rag_context", "") or "（无额外规则上下文）"
    enriched_context = state.get("enriched_context", "") or "（无额外历史信息）"

    prompt = ChatPromptTemplate.from_messages([
        ("system", BEHAVIOR_ANALYSIS_SYSTEM),
        ("human", BEHAVIOR_ANALYSIS_USER),
    ])

    llm = await get_llm(model_type="fast")
    llm = llm.with_structured_output(BehaviorProfile, method="json_mode")
    chain = prompt | llm

    try:
        import asyncio

        profile: BehaviorProfile = await asyncio.wait_for(
            chain.ainvoke({
                "snapshot_text": snapshot_text,
                "rag_context": rag_context,
                "enriched_context": enriched_context,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )
        logger.info(
            "[behavior_analysis] 分析完成, playstyle=%s, engagement=%s",
            profile.playstyle,
            profile.engagement_level,
        )
        return {"behavior_report": profile.model_dump_json()}
    except Exception as e:
        logger.error("[behavior_analysis] 行为分析失败: %s", e)
        return {
            "behavior_report": "",
            "errors": [f"行为分析失败: {e}"],
        }


# 节点5 行动推理
async def action_reasoning_node(state: AnalysisState) -> dict[str, Any]:
    """使用主力模型进行深度推理，输出 list[RecommendedAction]。

    同样使用 with_structured_output，返回已验证的 Pydantic 列表。
    读取 tracking_summary 和 anomalies，让 LLM 感知上次行动完成情况和当前异常。
    """
    snapshot = state.get("snapshot", {})
    snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)
    rag_context = state.get("rag_context", "") or "（无额外规则上下文）"
    enriched_context = state.get("enriched_context", "") or "（无额外历史信息）"
    behavior_report = state.get("behavior_report", "")
    tracking_summary = state.get("tracking_summary", "") or "（无行动追踪记录，首次分析）"
    anomalies = state.get("anomalies", [])
    anomaly_text = "\n".join(anomalies) if anomalies else "（无异常）"
    intent_result = state.get("intent_result") or {}
    goal_evaluation_result = state.get("goal_evaluation_result") or {}
    intent_text = json.dumps(intent_result, ensure_ascii=False) if intent_result else "（无意图推断数据）"
    goal_eval_text = (
        json.dumps(goal_evaluation_result, ensure_ascii=False)
        if goal_evaluation_result
        else "（无目标校验数据）"
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", ACTION_REASONING_SYSTEM),
        ("human", ACTION_REASONING_USER),
    ])

    llm = await get_llm(model_type="default")
    llm = llm.with_structured_output(ActionList, method="json_mode")
    chain = prompt | llm

    try:
        import asyncio

        action_list: ActionList | None = await asyncio.wait_for(
            chain.ainvoke({
                "behavior_report": behavior_report,
                "snapshot_text": snapshot_text,
                "rag_context": rag_context,
                "enriched_context": enriched_context,
                "tracking_summary": tracking_summary,
                "anomaly_text": anomaly_text,
                "intent_result": intent_text,
                "goal_evaluation_result": goal_eval_text,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )
        if action_list is None or not hasattr(action_list, "actions"):
            logger.warning("[action_reasoning] LLM 返回空结果，返回空列表")
            return {"reasoned_actions": [], "errors": ["行动推理返回空结果"]}
        logger.info(
            "[action_reasoning] 推理完成, actions_count=%d", len(action_list.actions)
        )
        return {"reasoned_actions": [a.model_dump(by_alias=True) for a in action_list.actions]}
    except Exception as e:
        logger.error("[action_reasoning] 行动推理失败: %s", e)
        return {
            "reasoned_actions": [],
            "errors": [f"行动推理失败: {e}"],
        }


# 节点6 组装输出
async def merge_output_node(state: AnalysisState) -> dict[str, Any]:
    """合并行为分析和推理结果为最终 PlayerAnalysisOutput。

    由于上游已使用 with_structured_output，这里直接组装已验证的模型，
    不需要 try/except json.loads。
    """
    behavior_report = state.get("behavior_report", "")
    reasoned_actions = state.get("reasoned_actions", [])

    try:
        profile = BehaviorProfile.model_validate_json(behavior_report)
        actions = [RecommendedAction.model_validate(a) for a in reasoned_actions]

        output = PlayerAnalysisOutput(
            player_profile=profile,
            recommended_actions=actions,
        )

        logger.info(
            "[merge_output] 组装完成, profile=%s, actions=%d",
            profile.playstyle,
            len(actions),
        )
        return {"final_output": output.model_dump(mode="json", by_alias=True)}
    except Exception as e:
        logger.error("[merge_output] 输出组装失败: %s", e)
        return {
            "final_output": {},
            "errors": [f"输出组装失败: {e}"],
        }


# 节点7 更新行动追踪记录（监督机制）
async def tracking_update_node(state: AnalysisState) -> dict[str, Any]:
    """更新行动追踪记录。

    两个职责：
    1. 将上次追踪中已完成/超时的行动状态持久化到数据库
    2. 将本次 final_output 中有 goal_metric 的行动写入新追踪记录

    只处理有 goal_metric 的行动，无法量化完成条件的行动不追踪。
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from src.core.agents.tools import _extract_metric
    from src.core.infrastructure.db import get_session

    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    snapshot = state.get("snapshot", {})
    final_output = state.get("final_output", {})
    actions = final_output.get("recommended_actions", [])
    abandoned_tracking_ids = set(state.get("abandoned_tracking_ids", []))

    now = datetime.now(UTC)

    # ── 步骤1：更新旧追踪记录状态（独立事务）──
    old_rows_count = 0
    try:
        async with get_session() as session:
            result = await session.execute(
                text("""
                    SELECT id, goal_metric, goal_value, baseline_value, deadline
                    FROM action_tracking
                    WHERE user_id = :user_id
                      AND tenant_id = :tenant_id
                      AND status = 'tracking'
                """),
                {"user_id": user_id, "tenant_id": tenant_id},
            )
            old_rows = result.fetchall()
            old_rows_count = len(old_rows)

            for row in old_rows:
                new_status = None
                completed_at = None
                completion_snapshot = None

                # LLM 判断目标已放弃（优先级最高）
                if str(row.id) in abandoned_tracking_ids:
                    new_status = "abandoned"

                # 指标对比判断完成
                elif row.goal_metric and row.goal_value is not None:
                    current_val = _extract_metric(snapshot, row.goal_metric)
                    if current_val is not None and current_val >= row.goal_value:
                        new_status = "completed"
                        completed_at = now
                        completion_snapshot = {row.goal_metric: current_val}

                # 截止时间判断超时（abandoned 和 completed 不再检查）
                if new_status is None and row.deadline:
                    deadline_dt = (
                        row.deadline
                        if row.deadline.tzinfo
                        else row.deadline.replace(tzinfo=UTC)
                    )
                    if now > deadline_dt:
                        new_status = "timeout"

                if new_status:
                    await session.execute(
                        text("""
                            UPDATE action_tracking
                            SET status = :status,
                                completed_at = :completed_at,
                                completion_snapshot = :completion_snapshot,
                                updated_at = :now
                            WHERE id = :id
                        """),
                        {
                            "status": new_status,
                            "completed_at": completed_at,
                            "completion_snapshot": completion_snapshot,
                            "now": now,
                            "id": row.id,
                        },
                    )
    except Exception as e:
        logger.error("[tracking_update] 旧记录状态更新失败: %s", e)
        return {"errors": [f"追踪记录状态更新失败: {e}"]}

    # ── 步骤2：写入本次可追踪行动（独立事务）──
    inserted = 0
    try:
        async with get_session() as session:
            trackable = [a for a in actions if a.get("goal_metric")]
            for action in trackable:
                goal_metric = action["goal_metric"]
                goal_value = action.get("goal_value")
                expected_hours = action.get("expected_hours")
                baseline_value = _extract_metric(snapshot, goal_metric)

                deadline = None
                if expected_hours:
                    deadline = now + timedelta(hours=expected_hours)

                await session.execute(
                    text("""
                        INSERT INTO action_tracking (
                            tenant_id, user_id, action_type, action_desc,
                            goal_metric, goal_value, baseline_value,
                            expected_hours, deadline, status, created_at, updated_at
                        ) VALUES (
                            :tenant_id, :user_id, :action_type, :action_desc,
                            :goal_metric, :goal_value, :baseline_value,
                            :expected_hours, :deadline, 'tracking', :now, :now
                        )
                    """),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "action_type": action.get("skillName") or action.get("skill_name", ""),
                        "action_desc": action.get("reason", ""),
                        "goal_metric": goal_metric,
                        "goal_value": goal_value,
                        "baseline_value": baseline_value,
                        "expected_hours": expected_hours,
                        "deadline": deadline,
                        "now": now,
                    },
                )
                inserted += 1
    except Exception as e:
        logger.error("[tracking_update] 新追踪记录写入失败: %s", e)
        return {"errors": [f"新追踪记录写入失败: {e}"]}

    logger.info(
        "[tracking_update] 完成, user_id=%s, 旧记录更新=%d, 新记录写入=%d",
        user_id,
        old_rows_count,
        inserted,
    )
    return {}
