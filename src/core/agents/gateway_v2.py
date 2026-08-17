from __future__ import annotations

import asyncio
import json
import logging
import operator
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict, cast

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

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
    fetch_snapshot_node,
    retrieve_rag_context_node,
)
from src.core.agents.state import AnalysisState
from src.core.integration.llm_gateway_v2.decision_service import (
    GatewayV2DecisionSelectionError,
    select_gateway_v2_action,
)
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields
from src.core.integration.llm_gateway_v2.token_usage import (
    gateway_v2_token_callback_config,
)
from src.core.llm.factory import get_llm

_SINGLE_CALL_TIMEOUT_SECONDS = 60
_MAX_STRUCTURED_OUTPUT_ATTEMPTS = 2
logger = logging.getLogger(__name__)


def _prompt_size_fields(name: str, text: str) -> dict[str, int]:
    try:
        import tiktoken

        estimated_tokens = len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        estimated_tokens = max(1, (len(text) + 3) // 4)
    return {
        f"{name}_chars": len(text),
        f"{name}_estimated_tokens": estimated_tokens,
    }


class GatewayV2AgentState(TypedDict, total=False):
    user_id: str
    tenant_id: str
    snapshot: dict[str, Any]
    gateway_context: dict[str, Any]
    rag_context: str
    reasoned_actions: Annotated[list[dict[str, Any]], operator.add]
    selected_action: dict[str, Any]
    errors: Annotated[list[str], operator.add]
    tracking_summary: str
    anomalies: Annotated[list[str], operator.add]
    abandoned_tracking_ids: Annotated[list[str], operator.add]
    player_memory: dict[str, Any]
    activity_plan: dict[str, Any] | None
    recent_action_history: list[dict[str, Any]]
    recent_failure_history: list[dict[str, Any]]
    current_step: dict[str, Any] | None


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
        gateway_context_payload = context.prompt_payload()
        gateway_context_text = json.dumps(gateway_context_payload, ensure_ascii=False)
        rag_context = state.get("rag_context") or "No RAG context"
        skill_catalog_text = json.dumps(
            {
                "availableSkills": gateway_context_payload["availableSkills"],
                "skillArgumentHints": gateway_context_payload["skillArgumentHints"],
            },
            ensure_ascii=False,
        )
        activity_plan_text = json.dumps(
            state.get("activity_plan"),
            ensure_ascii=False,
            default=str,
        )
        recent_action_history_text = json.dumps(
            state.get("recent_action_history", []),
            ensure_ascii=False,
            default=str,
        )
        recent_failure_history_text = json.dumps(
            state.get("recent_failure_history", []),
            ensure_ascii=False,
            default=str,
        )
        current_step_text = json.dumps(
            state.get("current_step"),
            ensure_ascii=False,
            default=str,
        )
        activity_context_text = "\n".join(
            (
                activity_plan_text,
                recent_action_history_text,
                recent_failure_history_text,
                current_step_text,
            )
        )
        prompt_values = {
            "gateway_context": gateway_context_text,
            "rag_context": rag_context,
            "activity_plan": activity_plan_text,
            "recent_action_history": recent_action_history_text,
            "recent_failure_history": recent_failure_history_text,
            "current_step": current_step_text,
        }
        final_prompt_text = "\n\n".join(
            str(message.content) for message in prompt.format_messages(**prompt_values)
        )
        prompt_size_fields = {
            **_prompt_size_fields("gateway_context", gateway_context_text),
            **_prompt_size_fields("skill_catalog", skill_catalog_text),
            **_prompt_size_fields("rag_context", rag_context),
            **_prompt_size_fields("activity_context", activity_context_text),
            **_prompt_size_fields("final_prompt", final_prompt_text),
        }
        logger.info(
            "[gateway_v2] prompt size breakdown: "
            "gateway_context_chars=%d gateway_context_estimated_tokens=%d "
            "skill_catalog_chars=%d skill_catalog_estimated_tokens=%d "
            "rag_context_chars=%d rag_context_estimated_tokens=%d "
            "activity_context_chars=%d activity_context_estimated_tokens=%d "
            "final_prompt_chars=%d final_prompt_estimated_tokens=%d "
            "prompt_token_estimator=%s",
            prompt_size_fields["gateway_context_chars"],
            prompt_size_fields["gateway_context_estimated_tokens"],
            prompt_size_fields["skill_catalog_chars"],
            prompt_size_fields["skill_catalog_estimated_tokens"],
            prompt_size_fields["rag_context_chars"],
            prompt_size_fields["rag_context_estimated_tokens"],
            prompt_size_fields["activity_context_chars"],
            prompt_size_fields["activity_context_estimated_tokens"],
            prompt_size_fields["final_prompt_chars"],
            prompt_size_fields["final_prompt_estimated_tokens"],
            "o200k_base_or_chars_div_4",
            extra={
                **prompt_size_fields,
                "prompt_token_estimator": "o200k_base_or_chars_div_4",
            },
        )
        raw_action_list: GatewayV2ActionList | None = None
        for attempt in range(1, _MAX_STRUCTURED_OUTPUT_ATTEMPTS + 1):
            try:
                callback_config = gateway_v2_token_callback_config()
                invocation = (
                    chain.ainvoke(prompt_values)
                    if callback_config is None
                    else chain.ainvoke(prompt_values, config=callback_config)
                )
                raw_action_list = await asyncio.wait_for(
                    invocation,
                    timeout=_SINGLE_CALL_TIMEOUT_SECONDS,
                )
                break
            except TimeoutError:
                raise
            except Exception as error:
                if attempt >= _MAX_STRUCTURED_OUTPUT_ATTEMPTS:
                    raise
                logger.warning(
                    "[gateway_v2] invalid structured action output; retrying: "
                    "event_id=%s attempt=%d exception_type=%s",
                    context.event_id,
                    attempt,
                    type(error).__name__,
                    extra={
                        "event_id": context.event_id,
                        "attempt": attempt,
                        "exception_type": type(error).__name__,
                    },
                )
        action_list = cast(GatewayV2ActionList | None, raw_action_list)
        if action_list is None or not action_list.actions:
            return {"reasoned_actions": [], "errors": ["gateway v2 action reasoning returned no actions"]}
        return {"reasoned_actions": [action.model_dump(mode="json", by_alias=True) for action in action_list.actions]}
    except Exception as error:
        event_id = context.event_id if "context" in locals() else None
        logger.error(
            "[gateway_v2] action reasoning failed",
            extra=safe_exception_fields(
                stage="agent",
                category="action_reasoning_failed",
                error=error,
                event_id=event_id,
            ),
        )
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
    builder.add_node("gateway_v2_action_reasoning", gateway_v2_action_reasoning_node)
    builder.add_node("gateway_v2_select_action", gateway_v2_select_action_node)

    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "retrieve_rag_context")
    builder.add_edge("retrieve_rag_context", "gateway_v2_action_reasoning")
    builder.add_edge("gateway_v2_action_reasoning", "gateway_v2_select_action")
    builder.add_edge("gateway_v2_select_action", END)
    return builder
