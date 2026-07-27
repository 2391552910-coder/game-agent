from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from scripts.assert_gateway_v2_state import parse_recovery_evidence, parse_session_evidence
from scripts.seed_gateway_v2_test_tenant import parse_seed_identity
from scripts.v2_e2e_common import require_test_database_url
from src.core.integration.llm_gateway_v2.contracts import GatewayV2BatchEnvelope

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SIMULATION_DRIVER_PATH = (
    _PROJECT_ROOT
    / ".codex"
    / "skills"
    / "myagent2-sgai-http-e2e"
    / "scripts"
    / "simulation_driver.py"
)
_SKILL_RUNNER_PATH = _SIMULATION_DRIVER_PATH.with_name("Invoke-MyAgent2SgaiHttpE2E.ps1")
_REAL_DRIVER_PATH = _PROJECT_ROOT / "scripts" / "invoke_gateway_v2_e2e.py"


def _load_script(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def simulation_driver() -> ModuleType:
    return _load_script("test_simulation_driver", _SIMULATION_DRIVER_PATH)


def test_test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_POSTGRES_DSN", raising=False)

    with pytest.raises(RuntimeError, match="TEST_POSTGRES_DSN is required"):
        require_test_database_url()


@pytest.mark.parametrize(
    "dsn",
    [
        "sqlite:///tmp/test.db",
        "postgresql+asyncpg://user:password@localhost/myagent",
        "postgresql+asyncpg://user:password@localhost/test_myagent",
    ],
)
def test_non_isolated_database_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
) -> None:
    monkeypatch.setenv("TEST_POSTGRES_DSN", dsn)

    with pytest.raises(RuntimeError, match="myagent_test_"):
        require_test_database_url()


def test_seed_identity_requires_uuid_and_bounded_gateway_id() -> None:
    tenant_id, gateway_id = parse_seed_identity(
        "00000000-0000-0000-0000-000000000001",
        "sgai-v2-e2e",
    )

    assert tenant_id == UUID("00000000-0000-0000-0000-000000000001")
    assert gateway_id == "sgai-v2-e2e"

    with pytest.raises(ValueError, match="tenant"):
        parse_seed_identity("not-a-uuid", "sgai-v2-e2e")
    with pytest.raises(ValueError, match="gateway"):
        parse_seed_identity(str(tenant_id), "")


def test_session_evidence_rejects_secret_or_raw_body_fields() -> None:
    evidence = {
        "sessionId": "session-1",
        "gatewayId": "sgai-v2-e2e",
        "controlGeneration": 1,
        "eventIdsByType": {
            "session_started": ["event-1"],
            "skill_started": ["event-2"],
            "skill_finished": ["event-3"],
            "session_stopped": ["event-4"],
        },
        "decisionIds": ["decision-1", "decision-2"],
        "skillCallIds": ["call-1"],
        "metricsBefore": {"llmEventsFailed": 0},
        "metricsAfter": {"llmEventsFailed": 0},
    }

    parsed = parse_session_evidence(evidence)

    assert parsed.session_id == "session-1"
    assert parsed.decision_ids == ("decision-1", "decision-2")

    for forbidden in ("appSecret", "requestBody", "rawBody", "request_body_bytes"):
        with pytest.raises(ValueError, match="forbidden"):
            parse_session_evidence({**evidence, forbidden: "sensitive"})


def test_v2_simulation_uses_separate_event_decision_and_control_identities(
    simulation_driver: ModuleType,
) -> None:
    state = simulation_driver.SimulationState(
        contract_version=simulation_driver.V2_CONTRACT_VERSION,
        event_app_id="event-app",
        event_app_secret="event-secret",
        decision_app_id="decision-app",
        decision_app_secret="decision-secret",
        control_app_id="control-app",
        control_app_secret="control-secret",
        gateway_id="sgai-v2-e2e",
        myagent_url="http://127.0.0.1:8000",
        run_id="identity-test",
    )

    assert simulation_driver._identity_for_path(state, simulation_driver.DECISION_PATH) == (
        "decision-app",
        "decision-secret",
    )
    assert simulation_driver._identity_for_path(state, simulation_driver.STATUS_PATH) == (
        "control-app",
        "control-secret",
    )
    assert simulation_driver._identity_for_path(state, simulation_driver.METRICS_PATH) == (
        "control-app",
        "control-secret",
    )


def test_e2e_skill_has_no_tracked_default_secret_and_generates_simulation_credentials() -> None:
    source = _SKILL_RUNNER_PATH.read_text(encoding="utf-8")

    assert "robot-gateway-smoke-secret" not in source
    assert "[string]$AppSecret = ''" in source
    assert "function New-RunScopedSecret" in source
    assert "E2E_EVENT_APP_SECRET" in source
    assert "E2E_DECISION_APP_SECRET" in source


def test_simulated_gateway_rejects_event_identity_on_decision_endpoint(
    simulation_driver: ModuleType,
) -> None:
    state = simulation_driver.SimulationState(
        contract_version=simulation_driver.V2_CONTRACT_VERSION,
        event_app_id="event-app",
        event_app_secret="event-secret",
        decision_app_id="decision-app",
        decision_app_secret="decision-secret",
        control_app_id="control-app",
        control_app_secret="control-secret",
        gateway_id="sgai-v2-e2e",
        myagent_url="http://127.0.0.1:8000",
        run_id="swapped-identity-test",
    )
    server = simulation_driver.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        simulation_driver._make_handler(state),
    )
    thread = simulation_driver.threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, response = simulation_driver._post_json(
            f"http://127.0.0.1:{server.server_port}",
            simulation_driver.DECISION_PATH,
            {},
            state.event_app_id,
            state.event_app_secret,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 401
    assert response["error"]["code"] == "signature_invalid"


def test_v2_simulation_builds_the_four_event_control_cycle(
    simulation_driver: ModuleType,
) -> None:
    state = simulation_driver.SimulationState(
        contract_version=simulation_driver.V2_CONTRACT_VERSION,
        event_app_id="event-app",
        event_app_secret="event-secret",
        decision_app_id="decision-app",
        decision_app_secret="decision-secret",
        control_app_id="control-app",
        control_app_secret="control-secret",
        gateway_id="sgai-v2-e2e",
        myagent_url="http://127.0.0.1:8000",
        run_id="contract-test",
    )

    batches = [
        simulation_driver._v2_event_batch(state, event_type="session_started", sequence=1),
        simulation_driver._v2_event_batch(
            state,
            event_type="skill_started",
            sequence=2,
            decision_id="decision-1",
            skill_call_id="skill-call-1",
        ),
        simulation_driver._v2_event_batch(
            state,
            event_type="skill_finished",
            sequence=3,
            decision_id="decision-1",
            skill_call_id="skill-call-1",
        ),
        simulation_driver._v2_event_batch(state, event_type="session_stopped", sequence=4),
    ]

    parsed = [GatewayV2BatchEnvelope.model_validate(batch) for batch in batches]

    assert [batch.events[0].event_type for batch in parsed] == [
        "session_started",
        "skill_started",
        "skill_finished",
        "session_stopped",
    ]
    assert [batch.events[0].event_sequence for batch in parsed] == [1, 2, 3, 4]


def test_v2_simulation_evidence_contains_only_database_assertion_fields(
    simulation_driver: ModuleType,
) -> None:
    state = simulation_driver.SimulationState(
        contract_version=simulation_driver.V2_CONTRACT_VERSION,
        event_app_id="event-app",
        event_app_secret="event-secret",
        decision_app_id="decision-app",
        decision_app_secret="decision-secret",
        control_app_id="control-app",
        control_app_secret="control-secret",
        gateway_id="sgai-v2-e2e",
        myagent_url="http://127.0.0.1:8000",
        run_id="evidence-test",
    )
    state.event_ids_by_type = {
        "session_started": ["event-1"],
        "skill_started": ["event-2"],
        "skill_finished": ["event-3"],
        "session_stopped": ["event-4"],
    }
    state.decisions = [{"decisionId": "decision-1"}, {"decisionId": "decision-2"}]
    state.skill_call_ids = ["skill-call-1", "skill-call-2"]
    metrics_before = state.snapshot_metrics()
    state.metrics["llmEventsSent"] = 4
    state.metrics["llmDecisionsAccepted"] = 2

    evidence = simulation_driver._v2_evidence(state, metrics_before)

    assert set(evidence) == {
        "sessionId",
        "gatewayId",
        "controlGeneration",
        "eventIdsByType",
        "decisionIds",
        "skillCallIds",
        "metricsBefore",
        "metricsAfter",
    }
    assert json.loads(json.dumps(evidence))["decisionIds"] == ["decision-1", "decision-2"]
    assert parse_session_evidence(evidence).session_id == state.session_id


def test_real_v2_driver_reads_only_control_plane_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _REAL_DRIVER_PATH.is_file(), "real v2 E2E driver is required"
    driver = _load_script("test_real_v2_driver_credentials", _REAL_DRIVER_PATH)
    monkeypatch.setenv("E2E_GATEWAY_CONTROL_APP_ID", "control-app")
    monkeypatch.setenv("E2E_GATEWAY_CONTROL_APP_SECRET", "control-secret")
    monkeypatch.setenv("E2E_EVENT_APP_ID", "must-not-be-read")
    monkeypatch.setenv("E2E_EVENT_APP_SECRET", "must-not-be-read")
    monkeypatch.setenv("E2E_DECISION_APP_ID", "must-not-be-read")
    monkeypatch.setenv("E2E_DECISION_APP_SECRET", "must-not-be-read")

    identity = driver.ControlIdentity.from_environment()

    assert identity.app_id == "control-app"
    assert identity.app_secret == "control-secret"


def test_real_v2_driver_builds_minimal_database_backed_evidence() -> None:
    assert _REAL_DRIVER_PATH.is_file(), "real v2 E2E driver is required"
    driver = _load_script("test_real_v2_driver_evidence", _REAL_DRIVER_PATH)

    evidence = driver.build_session_evidence(
        gateway_id="sgai-v2-e2e",
        session_id="session-1",
        control_generation=2,
        events=[
            {"event_id": "event-1", "event_type": "session_started"},
            {"event_id": "event-2", "event_type": "skill_started"},
            {"event_id": "event-3", "event_type": "skill_finished"},
            {"event_id": "event-4", "event_type": "session_stopped"},
        ],
        decisions=[{"decision_id": "decision-1"}, {"decision_id": "decision-2"}],
        skill_calls=[
            {"skill_call_id": "call-1", "status": "succeeded"},
            {"skill_call_id": "call-2", "status": "cancelled"},
        ],
        metrics_before={"llmDecisionsAccepted": 7},
        metrics_after={"llmDecisionsAccepted": 9},
    )

    assert set(evidence) == {
        "sessionId",
        "gatewayId",
        "controlGeneration",
        "eventIdsByType",
        "decisionIds",
        "skillCallIds",
        "metricsBefore",
        "metricsAfter",
    }
    assert evidence["skillCallIds"] == ["call-1"]
    assert parse_session_evidence(evidence).decision_ids == ("decision-1", "decision-2")


def test_recovery_evidence_requires_gap_and_generation_fence_proof() -> None:
    evidence = {
        "sessionId": "session-recovery",
        "gatewayId": "sgai-v2-e2e",
        "gapProbe": {
            "controlGeneration": 1,
            "eventIds": ["gap-sequence-1", "gap-sequence-2"],
        },
        "oldGenerationProbe": {
            "oldGeneration": 1,
            "newGeneration": 2,
            "lateEventIds": ["old-late", "old-stop"],
            "newStateVersion": 9,
            "newLeaseId": "new-lease",
            "newContextHash": "a" * 64,
            "decisionCountBeforeLateEvents": 1,
            "decisionCountAfterLateEvents": 1,
            "callbackCountBeforeLateEvents": 1,
            "callbackCountAfterLateEvents": 1,
        },
    }

    parsed = parse_recovery_evidence(evidence)

    assert parsed.gap.event_ids == ("gap-sequence-1", "gap-sequence-2")
    assert parsed.old_generation.new_generation == 2

    evidence["oldGenerationProbe"]["newContext"] = {"forbidden": "raw context"}
    with pytest.raises(ValueError, match="forbidden"):
        parse_recovery_evidence(evidence)
