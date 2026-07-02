"""测试 LangGraph 完整线性流程，并 print 输出 result["final_output"]。

用法:
    uv run python scripts/test_langgraph_final_output_mock.py
"""

import asyncio
import json
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.agents.models import BehaviorProfile, PlayerAnalysisOutput, RecommendedAction
from src.core.agents.state import AnalysisState


TEST_SNAPSHOT = {
    "user_id": "mock_player_001",
    "player_name": "测试玩家",
    "level": 18,
    "profession": "程序员",
    "current_area": "商业区",
    "target_area": "学习中心",
    "stats": {
        "play_hours": 42,
        "learning_courses": 2,
        "shopping_count": 6,
        "session_minutes": 95,
    },
    "recent_activities": ["学习编程课程", "购物", "健身"],
}


async def fetch_snapshot_node(state: AnalysisState) -> dict[str, Any]:
    if not state.get("snapshot"):
        return {"errors": ["snapshot为空，无法分析"]}
    return {}


async def retrieve_rag_context_node(state: AnalysisState) -> dict[str, Any]:
    snapshot = state["snapshot"]
    recent_activities = "、".join(snapshot.get("recent_activities", []))

    return {
        "rag_context": (
            f"{snapshot.get('current_area')}适合补给和社交；"
            f"近期行为包含：{recent_activities}。"
        )
    }


async def intent_inference_node(state: AnalysisState) -> dict[str, Any]:
    return {
        "intent_result": {
            "completed": ["完成商业区补给", "查看学习课程"],
            "abandoned": [],
            "next_likely": ["前往学习中心", "继续学习编程课程"],
            "intent_confidence": "medium",
            "session_summary": "玩家本次主要在商业区活动，并表现出继续学习编程课程的倾向。",
        }
    }


async def goal_evaluation_node(state: AnalysisState) -> dict[str, Any]:
    return {
        "goal_evaluation_result": {
            "has_active_goal": True,
            "goal_progress": 0.65,
            "cost_deviation": 1.05,
            "decision": "continue",
            "decision_reason": "玩家仍在推进学习类目标，当前进度正常，适合继续原目标。",
            "feasibility_issues": [],
            "suggested_goal": "继续完成下一节编程课程",
            "suggested_goal_type": "learning",
        }
    }


async def gather_context_node(state: AnalysisState) -> dict[str, Any]:
    snapshot = state["snapshot"]
    current_area = snapshot.get("current_area", "当前位置")
    target_area = snapshot.get("target_area", "目标位置")

    return {
        "enriched_context": f"玩家当前位置为{current_area}，下一个合理目标区域为{target_area}。",
        "tracking_summary": "上一次学习推荐尚未完成，但没有超时。",
        "anomalies": [],
        "abandoned_tracking_ids": [],
    }


async def behavior_analysis_node(state: AnalysisState) -> dict[str, Any]:
    snapshot = state["snapshot"]
    stats = snapshot.get("stats", {})

    profile = BehaviorProfile(
        playstyle="成长探索型",
        current_goal=[
            f"提升{snapshot.get('profession', '角色')}能力",
            "保持稳定日常活动",
        ],
        bottlenecks=[
            "学习课程数量偏少",
            "当前行动目标还不够聚焦",
        ],
        engagement_level="medium" if stats.get("session_minutes", 0) >= 60 else "low",
    )

    return {"behavior_report": profile.model_dump_json()}


async def action_reasoning_node(state: AnalysisState) -> dict[str, Any]:
    snapshot = state["snapshot"]

    action = RecommendedAction(
        skillName="move_to",
        priority="high",
        reason="玩家近期已经表现出学习倾向，继续安排课程能强化当前目标。",
        arguments={
            "target": {"x": 61.3, "y": 0.94, "z": 154.0},
            "stopDistance": 0.5,
            "label": snapshot.get("target_area", "学习中心"),
        },
        goal_metric="learning_courses",
        goal_value=3,
        expected_hours=24,
    )

    return {"reasoned_actions": [action.model_dump(mode="json", by_alias=True)]}


async def merge_output_node(state: AnalysisState) -> dict[str, Any]:
    action = RecommendedAction.model_validate(state["reasoned_actions"][0])
    output = PlayerAnalysisOutput(
        player_profile=BehaviorProfile.model_validate_json(state["behavior_report"]),
        recommended_actions=[action],
    )
    return {"final_output": output.model_dump(mode="json", by_alias=True)}


async def tracking_update_node(state: AnalysisState) -> dict[str, Any]:
    final_output = state.get("final_output", {})
    first_action = (final_output.get("recommended_actions") or [{}])[0]
    tracking_record = {
        "status": "tracking",
        "skillName": first_action.get("skillName"),
        "arguments": first_action.get("arguments", {}),
        "goal_metric": "learning_courses",
        "goal_value": 3,
    }

    return {"tracking_summary": json.dumps(tracking_record, ensure_ascii=False)}


async def memory_update_node(state: AnalysisState) -> dict[str, Any]:
    return {
        "player_memory": {
            "last_recommended_actions": state.get("final_output", {}).get("recommended_actions", []),
            "last_intent": state.get("intent_result", {}).get("session_summary", ""),
            "last_goal_decision": state.get("goal_evaluation_result", {}).get("decision", ""),
        }
    }


def build_mock_graph():
    builder = StateGraph(AnalysisState)

    builder.add_node("fetch_snapshot", fetch_snapshot_node)
    builder.add_node("retrieve_rag_context", retrieve_rag_context_node)
    builder.add_node("intent_inference", intent_inference_node)
    builder.add_node("goal_evaluation", goal_evaluation_node)
    builder.add_node("gather_context", gather_context_node)
    builder.add_node("behavior_analysis", behavior_analysis_node)
    builder.add_node("action_reasoning", action_reasoning_node)
    builder.add_node("merge_output", merge_output_node)
    builder.add_node("tracking_update", tracking_update_node)
    builder.add_node("memory_update", memory_update_node)

    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "retrieve_rag_context")
    builder.add_edge("retrieve_rag_context", "intent_inference")
    builder.add_edge("intent_inference", "goal_evaluation")
    builder.add_edge("goal_evaluation", "gather_context")
    builder.add_edge("gather_context", "behavior_analysis")
    builder.add_edge("behavior_analysis", "action_reasoning")
    builder.add_edge("action_reasoning", "merge_output")
    builder.add_edge("merge_output", "tracking_update")
    builder.add_edge("tracking_update", "memory_update")
    builder.add_edge("memory_update", END)

    return builder.compile()


async def main():
    graph = build_mock_graph()

    initial_state = {
        "user_id": TEST_SNAPSHOT["user_id"],
        "tenant_id": "mock_tenant_001",
        "snapshot": TEST_SNAPSHOT,
        "rag_context": "",
        "enriched_context": "",
        "behavior_report": "",
        "reasoned_actions": [],
        "final_output": {},
        "errors": [],
        "tracking_summary": "",
        "anomalies": [],
        "abandoned_tracking_ids": [],
        "intent_result": {},
        "goal_evaluation_result": {},
        "player_memory": {},
    }

    result = await graph.ainvoke(initial_state)
    final_output = result["final_output"]

    print(final_output)


if __name__ == "__main__":
    asyncio.run(main())
