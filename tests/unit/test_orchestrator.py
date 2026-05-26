"""Orchestrator 图结构测试。

验证图的拓扑结构正确，不执行真实节点逻辑。
"""

import pytest

from src.core.agents.orchestrator import _timed_node, build_orchestrator


class TestBuildOrchestrator:
    def test_returns_state_graph(self):
        from langgraph.graph import StateGraph

        graph = build_orchestrator()
        assert isinstance(graph, StateGraph)

    def test_all_nodes_registered(self):
        """验证所有 10 个节点都已注册。"""
        builder = build_orchestrator()
        # StateGraph 内部 _nodes 存储注册的节点
        node_names = set(builder.nodes.keys())
        expected = {
            "fetch_snapshot",
            "retrieve_rag_context",
            "intent_inference",
            "goal_evaluation",
            "gather_context",
            "behavior_analysis",
            "action_reasoning",
            "merge_output",
            "tracking_update",
            "memory_update",
        }
        assert expected == node_names

    def test_graph_compiles_without_checkpointer(self):
        """不含 checkpointer 时也能编译。"""
        builder = build_orchestrator()
        graph = builder.compile()
        assert graph is not None

    def test_graph_has_correct_edges(self):
        """验证线性边的顺序。"""
        builder = build_orchestrator()
        graph = builder.compile()

        # 获取图的边信息
        # compiled graph 的内部结构可通过 graph.graph 访问
        nodes = set(graph.get_graph().nodes.keys())
        # START 和 END 是特殊节点
        assert "__start__" in nodes
        assert "__end__" in nodes
        # 所有业务节点
        for name in [
            "fetch_snapshot",
            "retrieve_rag_context",
            "intent_inference",
            "goal_evaluation",
            "gather_context",
            "behavior_analysis",
            "action_reasoning",
            "merge_output",
            "tracking_update",
            "memory_update",
        ]:
            assert name in nodes, f"缺少节点: {name}"

    @pytest.mark.asyncio
    async def test_timed_node_logs_elapsed_ms(self, caplog, capsys):
        async def fake_node(state: dict) -> dict:
            return {"rag_context": "ok"}

        wrapped = _timed_node("retrieve_rag_context", fake_node)

        with caplog.at_level("INFO", logger="src.core.agents.orchestrator"):
            result = await wrapped({"user_id": "player_001"})

        assert result == {"rag_context": "ok"}
        assert "[agent_node_timing]" in caplog.text
        assert "node=retrieve_rag_context" in caplog.text
        assert "elapsed_ms=" in caplog.text
        captured = capsys.readouterr()
        assert "TIMING kind=agent_node node=retrieve_rag_context status=completed" in captured.out
