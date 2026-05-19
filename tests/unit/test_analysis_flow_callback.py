"""分析流程回调 RobotGateway 测试。"""

import logging

import pytest

from src.core.scheduler.flows import analysis_flow as flow_module


@pytest.fixture(autouse=True)
def prefect_logger(monkeypatch):
    monkeypatch.setattr(flow_module, "get_run_logger", lambda: logging.getLogger(__name__))


@pytest.mark.asyncio
async def test_send_callback_task_calls_robotgateway_callback(monkeypatch):
    calls = []

    async def fake_send_robotgateway_analysis_callback(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        flow_module,
        "send_robotgateway_analysis_callback",
        fake_send_robotgateway_analysis_callback,
        raising=False,
    )
    monkeypatch.setattr(
        flow_module.settings,
        "robotgateway_callback_url",
        "http://robotgateway.local/callbacks/analysis",
        raising=False,
    )
    monkeypatch.setattr(flow_module.settings, "robotgateway_callback_api_key", "secret", raising=False)
    monkeypatch.setattr(flow_module.settings, "robotgateway_callback_timeout_seconds", 10.0, raising=False)

    await flow_module.send_callback_task.fn(
        tenant_id="tenant_001",
        user_id="player_001",
        snapshot={"level": 28},
        output={"recommended_actions": []},
    )

    assert calls == [
        {
            "callback_url": "http://robotgateway.local/callbacks/analysis",
            "api_key": "secret",
            "timeout_seconds": 10.0,
            "tenant_id": "tenant_001",
            "user_id": "player_001",
            "snapshot": {"level": 28},
            "output": {"recommended_actions": []},
        }
    ]


@pytest.mark.asyncio
async def test_analysis_flow_stores_result_before_callback(monkeypatch):
    events = []

    async def fake_run_agent_task(user_id: str, tenant_id: str, snapshot: dict) -> dict:
        events.append("run_agent")
        return {"recommended_actions": []}

    async def fake_store_result_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None:
        events.append("store_result")

    async def fake_send_callback_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None:
        events.append("send_callback")

    monkeypatch.setattr(flow_module.run_agent_task, "fn", fake_run_agent_task)
    monkeypatch.setattr(flow_module.store_result_task, "fn", fake_store_result_task)
    monkeypatch.setattr(flow_module.send_callback_task, "fn", fake_send_callback_task)

    await flow_module.analysis_flow.fn(
        user_id="player_001",
        tenant_id="tenant_001",
        snapshot={"level": 28},
    )

    assert events == ["run_agent", "store_result", "send_callback"]
