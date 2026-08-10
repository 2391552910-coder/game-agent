"""Mock SGAI control plane used by the myAgent2 HTTP E2E skill."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

V1_CONTRACT_VERSION = "llm-gateway-http-v1"
V2_CONTRACT_VERSION = "llm-gateway-http-v2"
V1_EVENTS_PATH = "/api/gateway/events"
V2_EVENTS_PATH = "/api/gateway/v2/events"
LOGIN_PATH = "/api/v1/hosting/account-login-start"
STATUS_PATH = "/api/v1/hosting/status"
METRICS_PATH = "/api/v1/hosting/metrics"
DECISION_PATH = "/api/v1/hosting/llm/decision"
STOP_PATH = "/api/v1/hosting/stop"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _signature(secret: str, method: str, path: str, timestamp_ms: str, request_id: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join([method.upper(), path, timestamp_ms, request_id, body_hash])
    return hmac.new(secret.encode("utf-8"), signing_text.encode("utf-8"), hashlib.sha256).hexdigest()


def _signed_headers(app_id: str, secret: str, path: str, body: bytes) -> dict[str, str]:
    timestamp_ms = str(int(time.time() * 1000))
    request_id = f"req-{uuid4().hex}"
    return {
        "Content-Type": "application/json",
        "X-AppId": app_id,
        "X-TimestampMs": timestamp_ms,
        "X-RequestId": request_id,
        "X-Signature": _signature(secret, "POST", path, timestamp_ms, request_id, body),
    }


def _post_json(
    url: str,
    path: str,
    payload: dict[str, Any],
    app_id: str,
    secret: str,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    body = _json_bytes(payload)
    request = urllib.request.Request(
        f"{url}{path}",
        data=body,
        headers=_signed_headers(app_id, secret, path, body),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        status = exc.code

    try:
        parsed = json.loads(response_body.decode("utf-8")) if response_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path} returned invalid JSON with HTTP {status}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} returned a non-object JSON response")
    return status, parsed


def _get_json(url: str, path: str, timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(f"{url}{path}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        status = exc.code
    try:
        parsed = json.loads(response_body.decode("utf-8")) if response_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path} returned invalid JSON with HTTP {status}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} returned a non-object JSON response")
    return status, parsed


@dataclass
class SimulationState:
    contract_version: str
    event_app_id: str
    event_app_secret: str
    decision_app_id: str
    decision_app_secret: str
    control_app_id: str
    control_app_secret: str
    gateway_id: str
    myagent_url: str
    run_id: str
    trace_id: str = field(init=False)
    session_id: str = field(init=False)
    account_id: str = field(init=False)
    role_id: str = field(init=False)
    metrics: dict[str, int] = field(default_factory=lambda: {
        "llmEventsQueued": 0,
        "llmEventsSent": 0,
        "llmEventsFailed": 0,
        "llmEventsDropped": 0,
        "llmDecisionsAccepted": 0,
        "llmDecisionsRejected": 0,
    })
    expected_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    event_response: dict[str, Any] | None = None
    event_responses: list[dict[str, Any]] = field(default_factory=list)
    worker_error: str | None = None
    worker_started: bool = False
    stopped: bool = False
    event_ids_by_type: dict[str, list[str]] = field(default_factory=dict)
    skill_call_ids: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.trace_id = f"trace-{self.run_id}"
        self.session_id = f"session-{self.run_id}"
        self.account_id = f"account-{self.run_id}"
        self.role_id = f"role-{self.run_id}"

    def snapshot_metrics(self) -> dict[str, int]:
        with self.lock:
            return dict(self.metrics)

    @property
    def events_path(self) -> str:
        return V2_EVENTS_PATH if self.contract_version == V2_CONTRACT_VERSION else V1_EVENTS_PATH


def _identity_for_path(state: SimulationState, path: str) -> tuple[str, str]:
    if path == DECISION_PATH:
        return state.decision_app_id, state.decision_app_secret
    if path in {LOGIN_PATH, STATUS_PATH, METRICS_PATH, STOP_PATH}:
        return state.control_app_id, state.control_app_secret
    raise PermissionError("unsupported signed path")


def _session_payload(state: SimulationState) -> dict[str, Any]:
    return {
        "sessionId": state.session_id,
        "accountId": state.account_id,
        "roleId": state.role_id,
        "sceneId": 1001,
        "state": "Running",
        "position": {"x": 1.25, "y": 0.0, "z": -3.5},
        "controllable": True,
    }


def _event_batch(state: SimulationState, *, event_type: str, state_version: int, index: int) -> dict[str, Any]:
    if state.contract_version == V2_CONTRACT_VERSION:
        raise ValueError("use _v2_event_batch for the v2 contract")
    now_ms = int(time.time() * 1000)
    event_id = f"event-{state.run_id}-{index}"
    lease_id = f"lease-{state.run_id}-{index}"
    state.expected_decisions[lease_id] = {
        "traceId": state.trace_id,
        "sessionId": state.session_id,
        "stateVersion": state_version,
    }
    last_skill_result = None
    reason = "state_changed"
    if event_type == "skill_finished":
        reason = "ok"
        last_skill_result = {
            "skillCallId": f"skill-call-{state.run_id}-1",
            "skillName": "observe_state",
            "status": "success",
            "reason": "ok",
        }
    payload = {
        "eventType": event_type,
        "reason": reason,
        "session": _session_payload(state),
        "availableSkills": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "requireRunning": True,
                "cooldownMs": 0,
                "exposure": {"enabled": True},
            },
        ],
        "skillArgumentHints": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "argumentStatus": "ready",
                "allowedArgs": [],
                "missingArgs": [],
                "warnings": [],
                "stateRefs": [],
                "nextSteps": [],
            },
        ],
        "lastSkillResult": last_skill_result,
    }
    event = {
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": state.session_id,
        "stateVersion": state_version,
        "decisionLeaseId": lease_id,
        "occurredAtMs": now_ms,
        "payload": payload,
    }
    return {
        "traceId": state.trace_id,
        "gatewayId": state.gateway_id,
        "contractVersion": V1_CONTRACT_VERSION,
        "sentAtMs": now_ms,
        "events": [event],
    }


def _send_events(state: SimulationState, *, event_type: str, state_version: int, index: int) -> None:
    batch = _event_batch(state, event_type=event_type, state_version=state_version, index=index)
    event_count = len(batch["events"])
    with state.lock:
        state.metrics["llmEventsQueued"] += event_count
    try:
        status, response = _post_json(
            state.myagent_url,
            state.events_path,
            batch,
            state.event_app_id,
            state.event_app_secret,
            timeout=30.0,
        )
        expected_response = {
            "accepted": True,
            "traceId": state.trace_id,
            "receivedEventIds": [event["eventId"] for event in batch["events"]],
            "duplicateEventIds": [],
        }
        if status != 200 or response != expected_response:
            raise RuntimeError(f"events response mismatch: HTTP {status}, body={response}")
        with state.lock:
            state.event_response = response
            state.event_responses.append(response)
            state.metrics["llmEventsSent"] += event_count
    except Exception as exc:
        with state.lock:
            state.metrics["llmEventsFailed"] += event_count
            state.worker_error = str(exc)


def _v2_lease(state: SimulationState, *, state_version: int, index: int) -> dict[str, Any]:
    lease_id = f"lease-{state.run_id}-{index}"
    state.expected_decisions[lease_id] = {
        "stateVersion": state_version,
        "controlGeneration": 1,
    }
    return {
        "sessionId": state.session_id,
        "controlGeneration": 1,
        "decisionLeaseId": lease_id,
        "stateVersion": state_version,
        "leaseKind": "observation",
        "allowedActions": ["call_skill"],
        "allowedSkillName": "observe_state",
        "allowedSkillNames": ["observe_state"],
        "parentSkillName": None,
    }


def _v2_decision_context(state: SimulationState) -> dict[str, Any]:
    return {
        "session": {
            **_session_payload(state),
            "status": "active",
        },
        "availableSkills": [
            {
                "SkillName": "observe_state",
                "SchemaVersion": "v1",
                "RequireRunning": True,
                "CooldownMs": 0,
            }
        ],
        "skillArgumentHints": [
            {
                "skillName": "observe_state",
                "schemaVersion": "v1",
                "argumentStatus": "ready",
                "suggestedArgs": {},
                "allowedArgs": [],
                "missingArgs": [],
                "warnings": [],
                "nextSteps": [],
            }
        ],
        "lastSkillResult": None,
    }


def _v2_event_batch(
    state: SimulationState,
    *,
    event_type: str,
    sequence: int,
    decision_id: str | None = None,
    skill_call_id: str | None = None,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    event_id = f"event-{state.run_id}-{sequence}"
    state_version = 1
    decision_lease_id: str | None = None
    if event_type == "session_started":
        lease = _v2_lease(state, state_version=1, index=1)
        decision_lease_id = str(lease["decisionLeaseId"])
        payload: dict[str, Any] = {
            "reason": "decision_requested",
            "lease": lease,
            "decisionContext": _v2_decision_context(state),
        }
    elif event_type == "skill_started":
        payload = {
            "decisionId": decision_id,
            "skillName": "observe_state",
            "skillCallId": skill_call_id,
            "startedAtMs": now_ms,
        }
    elif event_type == "skill_finished":
        state_version = 2
        lease = _v2_lease(state, state_version=state_version, index=2)
        decision_lease_id = str(lease["decisionLeaseId"])
        payload = {
            "decisionId": decision_id,
            "skillName": "observe_state",
            "skillCallId": skill_call_id,
            "status": "success",
            "reason": "ok",
            "failureCategory": None,
            "retryable": False,
            "startedAtMs": now_ms - 1,
            "finishedAtMs": now_ms,
            "lease": lease,
            "decisionContext": _v2_decision_context(state),
        }
    elif event_type == "session_stopped":
        state_version = 2
        payload = {
            "reason": "simulation control stop",
            "stoppedAtMs": now_ms,
        }
    else:
        raise ValueError("unsupported v2 simulation event type")
    event = {
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": state.session_id,
        "controlGeneration": 1,
        "eventSequence": sequence,
        "stateVersion": state_version,
        "decisionLeaseId": decision_lease_id,
        "occurredAtMs": now_ms,
        "payload": payload,
    }
    with state.lock:
        state.event_ids_by_type.setdefault(event_type, []).append(event_id)
    return {
        "traceId": state.trace_id,
        "gatewayId": state.gateway_id,
        "contractVersion": V2_CONTRACT_VERSION,
        "sentAtMs": now_ms,
        "events": [event],
    }


def _send_v2_event(
    state: SimulationState,
    *,
    event_type: str,
    sequence: int,
    decision_id: str | None = None,
    skill_call_id: str | None = None,
) -> None:
    batch = _v2_event_batch(
        state,
        event_type=event_type,
        sequence=sequence,
        decision_id=decision_id,
        skill_call_id=skill_call_id,
    )
    with state.lock:
        state.metrics["llmEventsQueued"] += 1
    try:
        status, response = _post_json(
            state.myagent_url,
            V2_EVENTS_PATH,
            batch,
            state.event_app_id,
            state.event_app_secret,
            timeout=30.0,
        )
        expected = {
            "accepted": True,
            "traceId": state.trace_id,
            "receivedEventIds": [batch["events"][0]["eventId"]],
            "duplicateEventIds": [],
        }
        if status != 200 or response != expected:
            raise RuntimeError(f"v2 events response mismatch: HTTP {status}, body={response}")
        with state.lock:
            state.event_response = response
            state.event_responses.append(response)
            state.metrics["llmEventsSent"] += 1
    except Exception as exc:
        with state.lock:
            state.metrics["llmEventsFailed"] += 1
            state.worker_error = str(exc)


def _send_v2_skill_terminal(
    state: SimulationState,
    *,
    decision_id: str,
    skill_call_id: str,
) -> None:
    _send_v2_event(
        state,
        event_type="skill_started",
        sequence=2,
        decision_id=decision_id,
        skill_call_id=skill_call_id,
    )
    if state.worker_error is None:
        _send_v2_event(
            state,
            event_type="skill_finished",
            sequence=3,
            decision_id=decision_id,
            skill_call_id=skill_call_id,
        )


def _validate_decision(state: SimulationState, payload: dict[str, Any]) -> None:
    lease_id = payload.get("decisionLeaseId")
    expected = state.expected_decisions.get(lease_id)
    if expected is None:
        raise ValueError("unknown decisionLeaseId")
    for field_name, expected_value in expected.items():
        if payload.get(field_name) != expected_value:
            raise ValueError(f"{field_name} mismatch")
    if payload.get("contractVersion") != state.contract_version:
        raise ValueError("contractVersion mismatch")
    if state.contract_version == V1_CONTRACT_VERSION:
        if not isinstance(payload.get("reason"), str) or not payload["reason"]:
            raise ValueError("reason must be a non-empty string")
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise ValueError("confidence must be a JSON number")
        if not 0 <= float(confidence) <= 1:
            raise ValueError("confidence must be between 0 and 1")
    ttl_ms = payload.get("ttlMs")
    minimum_ttl = 1 if state.contract_version == V2_CONTRACT_VERSION else 0
    if type(ttl_ms) is not int or ttl_ms < minimum_ttl:
        raise ValueError(f"ttlMs must be an integer greater than or equal to {minimum_ttl}")
    if not isinstance(payload.get("decisionId"), str) or not payload["decisionId"]:
        raise ValueError("decisionId must be non-empty")
    if payload.get("action") != "call_skill":
        raise ValueError("action must be call_skill")
    if payload.get("skillName") != "observe_state":
        raise ValueError("skillName must be observe_state")
    if payload.get("schemaVersion") != "v1":
        raise ValueError("schemaVersion must be v1")
    if payload.get("arguments") != {}:
        raise ValueError("observe_state arguments must be an empty object")
    if any(item.get("decisionLeaseId") == lease_id for item in state.decisions):
        raise ValueError("decisionLeaseId was already consumed")


def _make_handler(state: SimulationState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MockSgai/1.0"

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_signed_json(self) -> dict[str, Any]:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            body = self.rfile.read(content_length)
            app_id = self.headers.get("X-AppId", "")
            timestamp_ms = self.headers.get("X-TimestampMs", "")
            request_id = self.headers.get("X-RequestId", "")
            supplied_signature = self.headers.get("X-Signature", "")
            expected_app_id, expected_secret = _identity_for_path(state, self.path)
            if app_id != expected_app_id or not timestamp_ms.isdigit() or not request_id:
                raise PermissionError("missing or invalid HMAC identity headers")
            if abs(int(time.time() * 1000) - int(timestamp_ms)) > 300_000:
                raise PermissionError("HMAC timestamp expired")
            expected_signature = _signature(
                expected_secret,
                "POST",
                self.path,
                timestamp_ms,
                request_id,
                body,
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise PermissionError("HMAC signature mismatch")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._read_signed_json()
                if self.path == LOGIN_PATH:
                    self._handle_login(payload)
                elif self.path == STATUS_PATH:
                    self._handle_status(payload)
                elif self.path == METRICS_PATH:
                    self._handle_metrics()
                elif self.path == DECISION_PATH:
                    self._handle_decision(payload)
                elif self.path == STOP_PATH:
                    self._handle_stop(payload)
                else:
                    self._send(404, {"error": "not_found"})
            except PermissionError as exc:
                self._send(401, {"error": {"code": "signature_invalid", "message": str(exc)}})
            except ValueError as exc:
                self._send(400, {"error": {"code": "bad_request", "message": str(exc)}})
            except Exception as exc:
                self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

        def _handle_login(self, payload: dict[str, Any]) -> None:
            if payload.get("gatewayId") != state.gateway_id:
                raise ValueError("gatewayId mismatch")
            with state.lock:
                if state.worker_started:
                    raise ValueError("session already started")
                state.worker_started = True
            self._send(200, {
                "sessionId": state.session_id,
                "accountId": state.account_id,
                "roleId": state.role_id,
                "state": "Starting",
            })
            if state.contract_version == V2_CONTRACT_VERSION:
                target = _send_v2_event
                kwargs = {"state": state, "event_type": "session_started", "sequence": 1}
            else:
                target = _send_events
                kwargs = {
                    "state": state,
                    "event_type": "observation_updated",
                    "state_version": 1,
                    "index": 1,
                }
            threading.Thread(target=target, kwargs=kwargs, daemon=True).start()

        def _handle_status(self, payload: dict[str, Any]) -> None:
            if payload.get("sessionId") != state.session_id:
                raise ValueError("sessionId mismatch")
            self._send(200, {
                "sessionId": state.session_id,
                "accountId": state.account_id,
                "roleId": state.role_id,
                "state": "Running",
            })

        def _handle_metrics(self) -> None:
            self._send(200, {"metrics": state.snapshot_metrics()})

        def _handle_decision(self, payload: dict[str, Any]) -> None:
            try:
                with state.lock:
                    _validate_decision(state, payload)
                    state.decisions.append(dict(payload))
                    state.metrics["llmDecisionsAccepted"] += 1
                    decision_index = len(state.decisions)
                    first_decision = decision_index == 1
                    skill_call_id = f"skill-call-{decision_index}-{state.run_id}"
                    state.skill_call_ids.append(skill_call_id)
            except ValueError:
                with state.lock:
                    state.metrics["llmDecisionsRejected"] += 1
                raise
            response = {
                "status": "accepted",
                "reason": "ok",
                "skillCallId": skill_call_id,
            }
            if state.contract_version == V2_CONTRACT_VERSION:
                response.update(
                    {
                        "accepted": True,
                        "traceId": payload["traceId"],
                        "sessionId": payload["sessionId"],
                        "decisionId": payload["decisionId"],
                        "controlGeneration": payload["controlGeneration"],
                        "stateVersion": payload["stateVersion"],
                        "nextDecisionLeaseId": None,
                    }
                )
            self._send(200, response)
            if first_decision:
                if state.contract_version == V2_CONTRACT_VERSION:
                    target = _send_v2_skill_terminal
                    kwargs = {
                        "state": state,
                        "decision_id": str(payload["decisionId"]),
                        "skill_call_id": skill_call_id,
                    }
                else:
                    target = _send_events
                    kwargs = {
                        "state": state,
                        "event_type": "skill_finished",
                        "state_version": 2,
                        "index": 2,
                    }
                threading.Thread(target=target, kwargs=kwargs, daemon=True).start()

        def _handle_stop(self, payload: dict[str, Any]) -> None:
            if payload.get("sessionId") != state.session_id:
                raise ValueError("sessionId mismatch")
            with state.lock:
                if state.stopped:
                    raise ValueError("session already stopped")
                state.stopped = True
            self._send(200, {
                "sessionId": state.session_id,
                "accountId": state.account_id,
                "roleId": state.role_id,
                "state": "Stopped",
            })
            if state.contract_version == V2_CONTRACT_VERSION:
                threading.Thread(
                    target=_send_v2_event,
                    kwargs={"state": state, "event_type": "session_stopped", "sequence": 4},
                    daemon=True,
                ).start()

    return Handler


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--myagent-port", type=int, required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--contract-version", choices=("v1", "v2"), default="v1")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    return parser.parse_args()


def _simulation_identity(role: str, *, contract_version: str) -> tuple[str, str]:
    prefix = f"SGAI_SIM_{role.upper()}"
    app_id = os.environ.get(f"{prefix}_APP_ID", "")
    app_secret = os.environ.get(f"{prefix}_APP_SECRET", "")
    if contract_version == V1_CONTRACT_VERSION:
        app_id = app_id or os.environ.get("SGAI_SIM_APP_ID", "")
        app_secret = app_secret or os.environ.get("SGAI_SIM_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError(f"required {role} simulation identity is missing")
    return app_id, app_secret


def _wait_for_metrics(
    state: SimulationState,
    gateway_url: str,
    *,
    timeout_seconds: float,
    events_sent: int,
    decisions_accepted: int,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, response = _post_json(
            gateway_url,
            METRICS_PATH,
            {},
            state.control_app_id,
            state.control_app_secret,
        )
        if status != 200:
            raise RuntimeError(f"metrics failed: HTTP {status}, body={response}")
        metrics = response.get("metrics")
        if not isinstance(metrics, dict):
            raise RuntimeError("metrics response is missing metrics")
        latest = metrics
        with state.lock:
            worker_error = state.worker_error
        if worker_error:
            raise RuntimeError(worker_error)
        if (
            metrics.get("llmEventsSent") == events_sent
            and metrics.get("llmEventsFailed") == 0
            and metrics.get("llmDecisionsAccepted") == decisions_accepted
            and metrics.get("llmDecisionsRejected") == 0
        ):
            return {key: int(value) for key, value in metrics.items() if type(value) is int}
        time.sleep(0.1)
    raise TimeoutError(f"bidirectional metrics did not converge: {latest}")


def _v2_evidence(state: SimulationState, metrics_before: dict[str, int]) -> dict[str, Any]:
    with state.lock:
        event_ids = {event_type: list(ids) for event_type, ids in state.event_ids_by_type.items()}
        decision_ids = [str(item["decisionId"]) for item in state.decisions]
        primary_skill_call_ids = list(state.skill_call_ids[:1])
        metrics_after = dict(state.metrics)
    return {
        "sessionId": state.session_id,
        "gatewayId": state.gateway_id,
        "controlGeneration": 1,
        "eventIdsByType": event_ids,
        "decisionIds": decision_ids,
        "skillCallIds": primary_skill_call_ids,
        "metricsBefore": metrics_before,
        "metricsAfter": metrics_after,
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    contract_version = V2_CONTRACT_VERSION if args.contract_version == "v2" else V1_CONTRACT_VERSION
    event_app_id, event_app_secret = _simulation_identity("event", contract_version=contract_version)
    decision_app_id, decision_app_secret = _simulation_identity(
        "decision", contract_version=contract_version
    )
    control_app_id, control_app_secret = _simulation_identity("control", contract_version=contract_version)
    if contract_version == V2_CONTRACT_VERSION and len(
        {event_app_id, decision_app_id, control_app_id}
    ) != 3:
        raise RuntimeError("v2 simulation event, decision, and control AppId values must be distinct")
    gateway_id = _required_environment("SGAI_SIM_GATEWAY_ID")
    gateway_url = f"http://127.0.0.1:{args.gateway_port}"
    state = SimulationState(
        contract_version=contract_version,
        event_app_id=event_app_id,
        event_app_secret=event_app_secret,
        decision_app_id=decision_app_id,
        decision_app_secret=decision_app_secret,
        control_app_id=control_app_id,
        control_app_secret=control_app_secret,
        gateway_id=gateway_id,
        myagent_url=f"http://127.0.0.1:{args.myagent_port}",
        run_id=args.run_id,
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.gateway_port), _make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        if contract_version == V2_CONTRACT_VERSION:
            capabilities_status, capabilities = _get_json(
                state.myagent_url,
                "/api/gateway/v2/capabilities",
            )
            if (
                capabilities_status != 200
                or capabilities.get("contractVersion") != V2_CONTRACT_VERSION
                or capabilities.get("receiveEventsPath") != V2_EVENTS_PATH
            ):
                raise RuntimeError(
                    f"v2 capabilities mismatch: HTTP {capabilities_status}, body={capabilities}"
                )

        metrics_before = _wait_for_metrics(
            state,
            gateway_url,
            timeout_seconds=args.timeout_seconds,
            events_sent=0,
            decisions_accepted=0,
        )
        login_body = {
            "gatewayId": gateway_id,
            "accountGroupId": f"simulation-{args.run_id}",
            "account": state.account_id,
            "loginMode": "account",
            "password": "simulation-only",
            "loginProfileKey": "simulation",
            "gameProfileKey": "simulation",
            "serverId": "0",
            "roleId": state.role_id,
        }
        login_status, login_response = _post_json(
            gateway_url,
            LOGIN_PATH,
            login_body,
            control_app_id,
            control_app_secret,
        )
        if login_status != 200 or login_response.get("sessionId") != state.session_id:
            raise RuntimeError(f"account-login-start failed: HTTP {login_status}, body={login_response}")

        status_code, status_response = _post_json(
            gateway_url,
            STATUS_PATH,
            {"sessionId": state.session_id, "accountId": state.account_id, "roleId": state.role_id},
            control_app_id,
            control_app_secret,
        )
        if status_code != 200 or status_response.get("state") != "Running":
            raise RuntimeError(f"status failed: HTTP {status_code}, body={status_response}")

        pre_stop_events = 3 if contract_version == V2_CONTRACT_VERSION else 2
        _wait_for_metrics(
            state,
            gateway_url,
            timeout_seconds=args.timeout_seconds,
            events_sent=pre_stop_events,
            decisions_accepted=2,
        )

        stop_status, stop_response = _post_json(
            gateway_url,
            STOP_PATH,
            {
                "sessionId": state.session_id,
                "accountId": state.account_id,
                "roleId": state.role_id,
                "reason": "simulation control stop",
            },
            control_app_id,
            control_app_secret,
        )
        if stop_status != 200 or stop_response.get("state") != "Stopped":
            raise RuntimeError(f"stop failed: HTTP {stop_status}, body={stop_response}")

        expected_events = 4 if contract_version == V2_CONTRACT_VERSION else 2
        metrics_after = _wait_for_metrics(
            state,
            gateway_url,
            timeout_seconds=args.timeout_seconds,
            events_sent=expected_events,
            decisions_accepted=2,
        )

        with state.lock:
            event_response = state.event_response
            event_responses = list(state.event_responses)
            decisions = list(state.decisions)
        if event_response is None or len(event_responses) != expected_events or len(decisions) != 2:
            raise RuntimeError("event response or decision records are incomplete")
        if contract_version == V2_CONTRACT_VERSION:
            return _v2_evidence(state, metrics_before)
        return {
            "success": True,
            "accountLoginState": login_response["state"],
            "hostingState": status_response["state"],
            "eventResponse": event_response,
            "eventResponses": event_responses,
            "metrics": metrics_after,
            "decisions": decisions,
        }
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)


def main() -> int:
    args = _parse_args()
    try:
        result = _run(args)
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 1
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
