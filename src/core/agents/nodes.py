"""LangGraph 节点函数。

图结构:
START
    → fetch_snapshot          获取玩家快照
    → retrieve_rag_context    主图统一 RAG 检索（一次检索，两节点共享）
    → behavior_analysis       行为分析（读 rag_context）
    → action_reasoning        行动推理（读 rag_context）
    → merge_output            组装最终结构化输出
END
"""

import json
import logging
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.core.agents.models import BehaviorProfile, PlayerAnalysisOutput, RecommendedAction
from src.core.agents.prompts import (
    ACTION_REASONING_SYSTEM,
    ACTION_REASONING_USER,
    BEHAVIOR_ANALYSIS_SYSTEM,
    BEHAVIOR_ANALYSIS_USER,
)
from src.core.agents.state import AnalysisState
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
        return {"errors": state["errors"] + ["snapshot为空，无法分析"]}

    logger.info("[fetch_snapshot] 快照数据验证通过，user_id=%s",state["user_id"])

    return {}

# 节点2 统一RAG检索
async def retrieve_rag_context_node(state:AnalysisState) -> dict[str, Any]:
    """
    主图统一做一次 RAG 检索，结果注入 rag_context。
    behavior_analysis 和 action_reasoning 共享同一份上下文，
    避免两次重复 IO。
    """
    snapshot = state.get("snapshot")
    json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)

    # 从快照中提取关键信息构建检索 query
    user_id = state["user_id"]
    query = f"玩家{user_id}的游戏规则和行为指导"

    try:
        from lightrag import QueryParam

        rag = await get_rag
        context = await rag.aquery(
            query,
            param=QueryParam(mode="hybrid")
        )
        logger.info(
              "[retrieve_rag_context] 检索完成, context_length=%d", len(context)
          )
        return {"rag_context", context}
    except Exception as e:
        logger.error("[retrieve_rag_context] RAG 检索失败: %s", e)
        return {
              "rag_context": "",
              "errors": state["errors"] + [f"RAG 检索失败: {e}"],
          }

# 节点3 行为分析
async def behavior_analysis_node(state: AnalysisState) -> dict[str, Any]:
    """
    使用快速模型分析玩家行为，输出 BehaviorProfile 结构化模型。
    使用 with_structured_output 确保 LLM 返回合法的 Pydantic 对象，
    而非需要 json.loads 的裸字符串。
    """

    snapshot = state.get("snapshot", {})
    snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)
    rag_context = state.get("rag_context", "") or "（无额外规则上下文）"

    prompt = ChatPromptTemplate.from_messages([
        ("system", BEHAVIOR_ANALYSIS_SYSTEM),
        ("human", BEHAVIOR_ANALYSIS_USER),
    ])

    # with_structured_output: LLM 被强制返回 BehaviorProfile 结构
    llm = get_llm(model_type="fast").with_structured_output(BehaviorProfile)
    chain = prompt | llm

    try:
        profile: BehaviorProfile = await chain.invoke({
            "snapshot_text", snapshot_text,
            "rag_context", rag_context,
        })
        logger.info(
            "[behavior_analysis]分析完成,playstyle=%s,engagement=%s",
            profile.playstyle,
            profile.engagement_level,
        )
        return {"behavior_report": profile.model_dump_json()}
    except Exception as e:
          logger.error("[behavior_analysis] 行为分析失败: %s", e)
          return {
              "behavior_report": "",
              "errors": state["errors"] + [f"行为分析失败: {e}"],
          }

# 节点4 行动推理
async def action_reasoning_node(state: AnalysisState) -> dict[str, Any]:
      """使用主力模型进行深度推理，输出 list[RecommendedAction]。

      同样使用 with_structured_output，返回已验证的 Pydantic 列表。
      """
      snapshot = state.get("snapshot", {})
      snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, dict) else str(snapshot)
      rag_context = state.get("rag_context", "") or "（无额外规则上下文）"
      behavior_report = state.get("behavior_report", "")

      prompt = ChatPromptTemplate.from_messages([
          ("system", ACTION_REASONING_SYSTEM),
          ("human", ACTION_REASONING_USER),
      ])

      # with_structured_output: LLM 被强制返回 list[RecommendedAction]
      llm = get_llm(model_type="default").with_structured_output(list[RecommendedAction])
      chain = prompt | llm

      try:
          actions: list[RecommendedAction] = await chain.ainvoke({
              "behavior_report": behavior_report,
              "snapshot_text": snapshot_text,
              "rag_context": rag_context,
          })
          logger.info(
              "[action_reasoning] 推理完成, actions_count=%d", len(actions)
          )
          return {"reasoned_actions": [a.model_dump() for a in actions]}
      except Exception as e:
          logger.error("[action_reasoning] 行动推理失败: %s", e)
          return {
              "reasoned_actions": [],
              "errors": state["errors"] + [f"行动推理失败: {e}"],
          }

async def merge_output_node(state: AnalysisState) -> dict[str, Any]:
    """合并行为分析和推理结果为最终 PlayerAnalysisOutput。

    由于上游已使用 with_structured_output，这里直接组装已验证的模型，
    不需要 try/except json.loads。
    """
    behavior_report = state.get("behavior_report", "")
    reasoned_actions = state.get("reasoned_actions", [])

    try:
        # behavior_report 是 BehaviorProfile 的 JSON 字符串
        profile = BehaviorProfile.model_validate_json(behavior_report)
        # reasoned_actions 已经是 list[dict]
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
            "errors": state["errors"] + [f"输出组装失败: {e}"],
        }
