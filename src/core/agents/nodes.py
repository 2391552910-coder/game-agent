"""LangGraph 节点函数。

图结构:
START
    → fetch_snapshot          获取玩家快照
    → retrieve_rag_context    主图统一 RAG 检索（一次检索，两节点共享）
    → gather_context          工具收集额外上下文（历史趋势等）
    → behavior_analysis       行为分析（读 rag_context + enriched_context）
    → action_reasoning        行动推理（读 rag_context + enriched_context）
    → merge_output            组装最终结构化输出
END
"""

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate

# ── 防循环/防卡死常量 ──
# gather_context: 最多 N 轮 LLM 对话（每轮可含多个 tool_call）
_MAX_GATHER_ITERATIONS = 3
# gather_context: 总工具调用次数上限（跨所有轮次累计）
_MAX_TOTAL_TOOL_CALLS = 8
# 单次外部调用超时（秒）: RAG 检索、LLM 调用、工具执行
_SINGLE_CALL_TIMEOUT = 60

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
from src.core.llm.factory import get_llm

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

    try:
        import asyncio

        from lightrag import QueryParam

        rag = await get_rag()
        context = await asyncio.wait_for(
            rag.aquery(query, param=QueryParam(mode="hybrid")),
            timeout=_SINGLE_CALL_TIMEOUT,
        )
        logger.info(
            "[retrieve_rag_context] 检索完成, context_length=%d, query=%s",
            len(context),
            query[:80],
        )
        return {"rag_context": context}
    except Exception as e:
        logger.error("[retrieve_rag_context] RAG 检索失败: %s", e)
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

    tools = create_tools(state["tenant_id"], state["user_id"])
    llm = await get_llm(model_type="fast")
    llm_with_tools = llm.bind_tools(tools)

    messages = [
        SystemMessage(content=CONTEXT_GATHERING_SYSTEM),
        HumanMessage(content=f"玩家快照:\n{snapshot_text}\n\n已有RAG上下文:\n{rag_context}"),
    ]

    enriched_parts: list[str] = []
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
                    except asyncio.TimeoutError:
                        logger.warning("[gather_context] 工具 %s 执行超时 (%ds)", tool_name, _SINGLE_CALL_TIMEOUT)
                        result = f"工具执行超时"
                    except Exception as e:
                        logger.error("[gather_context] 工具 %s 执行失败: %s", tool_name, e)
                        result = f"工具执行失败: {e}"

                enriched_parts.append(f"[{tool_name}] {result}")
                messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))

        enriched_context = "\n\n".join(enriched_parts) if enriched_parts else ""
        logger.info("[gather_context] 上下文收集完成, 轮次=%d", iteration_count)
        return {"enriched_context": enriched_context}

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
    llm = llm.with_structured_output(BehaviorProfile, method="function_calling")
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
    """
    snapshot = state.get("snapshot", {})
    snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)
    rag_context = state.get("rag_context", "") or "（无额外规则上下文）"
    enriched_context = state.get("enriched_context", "") or "（无额外历史信息）"
    behavior_report = state.get("behavior_report", "")

    prompt = ChatPromptTemplate.from_messages([
        ("system", ACTION_REASONING_SYSTEM),
        ("human", ACTION_REASONING_USER),
    ])

    llm = await get_llm(model_type="default")
    llm = llm.with_structured_output(ActionList, method="function_calling")
    chain = prompt | llm

    try:
        import asyncio

        action_list: ActionList | None = await asyncio.wait_for(
            chain.ainvoke({
                "behavior_report": behavior_report,
                "snapshot_text": snapshot_text,
                "rag_context": rag_context,
                "enriched_context": enriched_context,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )
        if action_list is None or not hasattr(action_list, 'actions'):
            logger.warning("[action_reasoning] LLM 返回空结果，返回空列表")
            return {"reasoned_actions": [], "errors": ["行动推理返回空结果"]}
        logger.info(
            "[action_reasoning] 推理完成, actions_count=%d", len(action_list.actions)
        )
        return {"reasoned_actions": [a.model_dump() for a in action_list.actions]}
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
        return {"final_output": output.model_dump(mode="json")}
    except Exception as e:
        logger.error("[merge_output] 输出组装失败: %s", e)
        return {
            "final_output": {},
            "errors": [f"输出组装失败: {e}"],
        }
