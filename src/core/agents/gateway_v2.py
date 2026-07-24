from __future__ import annotations

import asyncio
import json
import operator
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict, cast

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

from src.core.agents.decision_nodes import goal_evaluation_node, intent_inference_node
from src.core.agents.gateway_v2_models import (
    GatewayV2ActionList,
    GatewayV2AgentContext,
    parse_gateway_v2_agent_action,
)
from src.core.agents.gateway_v2_prompts import (
    GATEWAY_V2_ACTION_REASONING_SYSTEM,
    GATEWAY_V2_ACTION_REASONING_USER,
)
from src.core.agents.nodes import (
    behavior_analysis_node,
    fetch_snapshot_node,
    gather_context_node,
    retrieve_rag_context_node,
)
from src.core.agents.state import AnalysisState
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2DecisionSelectionError,
    select_gateway_v2_action,
)
from src.core.llm.factory import get_llm

_SINGLE_CALL_TIMEOUT_SECONDS = 60


class GatewayV2AgentState(TypedDict, total=False):
    user_id: str
    tenant_id: str
    snapshot: dict[str, Any]
    gateway_context: dict[str, Any]
    rag_context: str
    enriched_context: str
    behavior_report: str
    reasoned_actions: Annotated[list[dict[str, Any]], operator.add]
    selected_action: dict[str, Any]
    errors: Annotated[list[str], operator.add]
    tracking_summary: str
    anomalies: Annotated[list[str], operator.add]
    abandoned_tracking_ids: Annotated[list[str], operator.add]
    intent_result: dict[str, Any]
    goal_evaluation_result: dict[str, Any]
    player_memory: dict[str, Any]


def _adapt_read_node(
    node: Callable[[AnalysisState], Awaitable[dict[str, Any]]],
) -> Callable[[GatewayV2AgentState], Awaitable[GatewayV2AgentState]]:
    async def adapted(state: GatewayV2AgentState) -> GatewayV2AgentState:
        return cast(GatewayV2AgentState, await node(cast(AnalysisState, state)))

    return adapted


async def gateway_v2_action_reasoning_node(state: GatewayV2AgentState) -> GatewayV2AgentState:
    try:
        context = GatewayV2AgentContext.model_validate(state["gateway_context"])
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", GATEWAY_V2_ACTION_REASONING_SYSTEM),
                ("human", GATEWAY_V2_ACTION_REASONING_USER),
            ]
        )
        llm = await get_llm(model_type="default")
        structured_llm = llm.with_structured_output(GatewayV2ActionList, method="json_mode")
        chain = prompt | structured_llm
        raw_action_list = await asyncio.wait_for(
            chain.ainvoke(
                {
                    "gateway_context": json.dumps(context.prompt_payload(), ensure_ascii=False),
                    "behavior_report": state.get("behavior_report", ""),
                    "snapshot_text": json.dumps(state.get("snapshot", {}), ensure_ascii=False),
                    "rag_context": state.get("rag_context") or "No RAG context",
                    "enriched_context": state.get("enriched_context") or "No enriched context",
                    "intent_result": json.dumps(state.get("intent_result", {}), ensure_ascii=False),
                    "goal_evaluation_result": json.dumps(
                        state.get("goal_evaluation_result", {}),
                        ensure_ascii=False,
                    ),
                }
            ),
            timeout=_SINGLE_CALL_TIMEOUT_SECONDS,
        )
        action_list = cast(GatewayV2ActionList | None, raw_action_list)
        if action_list is None or not action_list.actions:
            return {"reasoned_actions": [], "errors": ["gateway v2 action reasoning returned no actions"]}
        return {"reasoned_actions": [action.model_dump(mode="json", by_alias=True) for action in action_list.actions]}
    except Exception:
        return {"reasoned_actions": [], "errors": ["gateway v2 action reasoning failed"]}


async def gateway_v2_select_action_node(state: GatewayV2AgentState) -> GatewayV2AgentState:
    if state.get("errors"):
        return {}
    try:
        context = GatewayV2AgentContext.model_validate(state["gateway_context"])
        candidates = [parse_gateway_v2_agent_action(item) for item in state.get("reasoned_actions", [])]
        if not candidates:
            return {"errors": ["gateway v2 action selection received no candidates"]}
        selected = select_gateway_v2_action(context, candidates)
        return {"selected_action": selected.model_dump(mode="json", by_alias=True)}
    except GatewayV2DecisionSelectionError:
        return {"errors": ["gateway v2 lease has no permitted decision"]}
    except Exception:
        return {"errors": ["gateway v2 action selection failed"]}


def build_gateway_v2_decision_graph() -> StateGraph[
    GatewayV2AgentState,
    None,
    GatewayV2AgentState,
    GatewayV2AgentState,
]:
    builder = StateGraph(GatewayV2AgentState)
    builder.add_node("fetch_snapshot", cast(Any, _adapt_read_node(fetch_snapshot_node)))
    builder.add_node(
        "retrieve_rag_context",
        cast(Any, _adapt_read_node(retrieve_rag_context_node)),
    )
    builder.add_node("intent_inference", cast(Any, _adapt_read_node(intent_inference_node)))
    builder.add_node("goal_evaluation", cast(Any, _adapt_read_node(goal_evaluation_node)))
    builder.add_node("gather_context", cast(Any, _adapt_read_node(gather_context_node)))
    builder.add_node("behavior_analysis", cast(Any, _adapt_read_node(behavior_analysis_node)))
    builder.add_node("gateway_v2_action_reasoning", gateway_v2_action_reasoning_node)
    builder.add_node("gateway_v2_select_action", gateway_v2_select_action_node)

    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "retrieve_rag_context")
    builder.add_edge("retrieve_rag_context", "intent_inference")
    builder.add_edge("intent_inference", "goal_evaluation")
    builder.add_edge("goal_evaluation", "gather_context")
    builder.add_edge("gather_context", "behavior_analysis")
    builder.add_edge("behavior_analysis", "gateway_v2_action_reasoning")
    builder.add_edge("gateway_v2_action_reasoning", "gateway_v2_select_action")
    builder.add_edge("gateway_v2_select_action", END)
    return builder
