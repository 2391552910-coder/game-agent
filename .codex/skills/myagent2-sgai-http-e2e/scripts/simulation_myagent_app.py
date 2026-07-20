"""Real myAgent2 FastAPI app with deterministic Agent output for HTTP contract tests."""

from typing import Any

from src.api.main import app
from src.api.routes import webhooks


async def _run_deterministic_gateway_agent(**_: Any) -> dict[str, Any]:
    return {
        "recommended_actions": [
            {
                "action": "call_skill",
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "arguments": {},
                "reason": "simulation contract probe",
                "priority": "high",
                "ttlMs": 30_000,
            }
        ]
    }


webhooks.run_gateway_v1_agent = _run_deterministic_gateway_agent
