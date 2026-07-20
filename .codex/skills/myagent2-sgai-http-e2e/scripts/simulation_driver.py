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
from typing import Any
from uuid import uuid4

CONTRACT_VERSION = "llm-gateway-http-v1"
EVENTS_PATH = "/api/gateway/events"
LOGIN_PATH = "/api/v1/hosting/account-login-start"
STATUS_PATH = "/api/v1/hosting/status"
METRICS_PATH = "/api/v1/hosting/metrics"
DECISION_PATH = "/api/v1/hosting/llm/decision"


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


@dataclass
class SimulationState:
    app_id: str
    app_secret: str
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
    worker_error: str | None = None
    worker_started: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.trace_id = f"trace-{self.run_id}"
        self.session_id = f"session-{self.run_id}"
        self.account_id = f"account-{self.run_id}"
        self.role_id = f"role-{self.run_id}"

    def snapshot_metrics(self) -> dict[str, int]:
        with self.lock:
            return dict(self.metrics)


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


def _event_batch(state: SimulationState) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    event_specs = [
        ("session_started", 1, {"session": _session_payload(state)}),
        (
            "observation_updated",
            2,
            {
                "session": _session_payload(state),
                "observation": {"reason": "state_changed"},
            },
        ),
    ]
    events: list[dict[str, Any]] = []
    for index, (event_type, state_version, payload) in enumerate(event_specs, start=1):
        event_id = f"event-{state.run_id}-{index}"
        lease_id = f"lease-{state.run_id}-{index}"
        state.expected_decisions[lease_id] = {
            "traceId": state.trace_id,
            "sessionId": state.session_id,
            "stateVersion": state_version,
        }
        events.append({
            "eventId": event_id,
            "eventType": event_type,
            "sessionId": state.session_id,
            "stateVersion": state_version,
            "decisionLeaseId": lease_id,
            "occurredAtMs": now_ms + index,
            "payload": payload,
        })
    return {
        "traceId": state.trace_id,
        "gatewayId": state.gateway_id,
        "contractVersion": CONTRACT_VERSION,
        "sentAtMs": now_ms,
        "events": events,
    }


def _send_events(state: SimulationState) -> None:
    batch = _event_batch(state)
    event_count = len(batch["events"])
    with state.lock:
        state.metrics["llmEventsQueued"] += event_count
    try:
        status, response = _post_json(
            state.myagent_url,
            EVENTS_PATH,
            batch,
            state.app_id,
            state.app_secret,
            timeout=30.0,
        )
        expected_results = [
            {"status": "accepted", "eventId": event["eventId"]}
            for event in batch["events"]
        ]
        expected_response = {
            "status": "accepted",
            "traceId": state.trace_id,
            "results": expected_results,
        }
        if status != 200 or response != expected_response:
            raise RuntimeError(f"events response mismatch: HTTP {status}, body={response}")
        with state.lock:
            state.event_response = response
            state.metrics["llmEventsSent"] += event_count
    except Exception as exc:
        with state.lock:
            state.metrics["llmEventsFailed"] += event_count
            state.worker_error = str(exc)


def _validate_decision(state: SimulationState, payload: dict[str, Any]) -> None:
    lease_id = payload.get("decisionLeaseId")
    expected = state.expected_decisions.get(lease_id)
    if expected is None:
        raise ValueError("unknown decisionLeaseId")
    for field_name, expected_value in expected.items():
        if payload.get(field_name) != expected_value:
            raise ValueError(f"{field_name} mismatch")
    if payload.get("contractVersion") != CONTRACT_VERSION:
        raise ValueError("contractVersion mismatch")
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
            if app_id != state.app_id or not timestamp_ms.isdigit() or not request_id:
                raise PermissionError("missing or invalid HMAC identity headers")
            if abs(int(time.time() * 1000) - int(timestamp_ms)) > 300_000:
                raise PermissionError("HMAC timestamp expired")
            expected_signature = _signature(
                state.app_secret,
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
            threading.Thread(target=_send_events, args=(state,), daemon=True).start()

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
            except ValueError:
                with state.lock:
                    state.metrics["llmDecisionsRejected"] += 1
                raise
            self._send(200, {
                "status": "accepted",
                "reason": "ok",
                "skillCallId": f"skill-call-{len(state.decisions)}-{state.run_id}",
            })

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
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    return parser.parse_args()


def _run() -> dict[str, Any]:
    args = _parse_args()
    app_id = _required_environment("SGAI_SIM_APP_ID")
    app_secret = _required_environment("SGAI_SIM_APP_SECRET")
    gateway_id = _required_environment("SGAI_SIM_GATEWAY_ID")
    gateway_url = f"http://127.0.0.1:{args.gateway_port}"
    state = SimulationState(
        app_id=app_id,
        app_secret=app_secret,
        gateway_id=gateway_id,
        myagent_url=f"http://127.0.0.1:{args.myagent_port}",
        run_id=args.run_id,
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.gateway_port), _make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
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
            gateway_url, LOGIN_PATH, login_body, app_id, app_secret
        )
        if login_status != 200 or login_response.get("sessionId") != state.session_id:
            raise RuntimeError(f"account-login-start failed: HTTP {login_status}, body={login_response}")

        status_code, status_response = _post_json(
            gateway_url,
            STATUS_PATH,
            {"sessionId": state.session_id, "accountId": state.account_id, "roleId": state.role_id},
            app_id,
            app_secret,
        )
        if status_code != 200 or status_response.get("state") != "Running":
            raise RuntimeError(f"status failed: HTTP {status_code}, body={status_response}")

        deadline = time.monotonic() + args.timeout_seconds
        metrics_response: dict[str, Any] = {}
        while time.monotonic() < deadline:
            metrics_status, metrics_response = _post_json(
                gateway_url, METRICS_PATH, {}, app_id, app_secret
            )
            metrics = metrics_response.get("metrics", {})
            if metrics_status != 200:
                raise RuntimeError(f"metrics failed: HTTP {metrics_status}, body={metrics_response}")
            with state.lock:
                worker_error = state.worker_error
            if worker_error:
                raise RuntimeError(worker_error)
            if (
                metrics.get("llmEventsSent") == 2
                and metrics.get("llmEventsFailed") == 0
                and metrics.get("llmDecisionsAccepted") == 2
                and metrics.get("llmDecisionsRejected") == 0
            ):
                break
            time.sleep(0.1)
        else:
            raise TimeoutError(f"bidirectional metrics did not converge: {metrics_response}")

        with state.lock:
            event_response = state.event_response
            decisions = list(state.decisions)
        if event_response is None or len(decisions) != 2:
            raise RuntimeError("event response or decision records are incomplete")
        return {
            "success": True,
            "accountLoginState": login_response["state"],
            "hostingState": status_response["state"],
            "eventResponse": event_response,
            "metrics": metrics_response["metrics"],
            "decisions": decisions,
        }
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)


def main() -> int:
    try:
        result = _run()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
