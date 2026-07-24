"""Real myAgent2 FastAPI app with deterministic Agent output for HTTP contract tests."""

from typing import Any

from src.api.main import app as app
from src.api.routes import webhooks
from src.core.agents.gateway_v2_models import GatewayV2CallSkillAction
from src.core.integration.llm_gateway_v2 import decision_service as gateway_v2_decision_service


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


async def _decide_deterministic_gateway_v2_action(
    _self: object,
    _context: object,
    *,
    user_id: str,
    tenant_id: str,
) -> GatewayV2CallSkillAction:
    del user_id, tenant_id
    return GatewayV2CallSkillAction.model_validate(
        {
            "action": "call_skill",
            "skillName": "observe_state",
            "schemaVersion": "v1",
            "arguments": {},
            "reason": "simulation contract probe",
            "ttlMs": 30_000,
        }
    )


gateway_v2_decision_service.GatewayV2DecisionService.decide = (
    _decide_deterministic_gateway_v2_action
)
