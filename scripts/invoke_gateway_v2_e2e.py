from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from scripts.v2_e2e_common import open_verified_test_engine, require_test_database_url

LOGIN_PATH = "/api/v1/hosting/account-login-start"
STATUS_PATH = "/api/v1/hosting/status"
METRICS_PATH = "/api/v1/hosting/metrics"
STOP_PATH = "/api/v1/hosting/stop"


@dataclass(frozen=True)
class ControlIdentity:
    app_id: str
    app_secret: str

    @classmethod
    def from_environment(cls) -> ControlIdentity:
        app_id = os.environ.get("E2E_GATEWAY_CONTROL_APP_ID", "")
        app_secret = os.environ.get("E2E_GATEWAY_CONTROL_APP_SECRET", "")
        if not app_id.strip() or not app_secret.strip():
            raise RuntimeError("Gateway control identity is required")
        return cls(app_id=app_id, app_secret=app_secret)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _signature(
    secret: str,
    method: str,
    path: str,
    timestamp_ms: str,
    request_id: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join((method.upper(), path, timestamp_ms, request_id, body_hash))
    return hmac.new(
        secret.encode("utf-8"),
        signing_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _post_json(
    base_url: str,
    path: str,
    payload: Mapping[str, Any],
    identity: ControlIdentity,
    *,
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    body = _json_bytes(payload)
    timestamp_ms = str(int(time.time() * 1_000))
    request_id = str(uuid4())
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AppId": identity.app_id,
            "X-TimestampMs": timestamp_ms,
            "X-RequestId": request_id,
            "X-Signature": _signature(
                identity.app_secret,
                "POST",
                path,
                timestamp_ms,
                request_id,
                body,
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    try:
        parsed = json.loads(response_body.decode("utf-8")) if response_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Gateway control response is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("Gateway control response must be a JSON object")
    return status, parsed


def _metrics(payload: Mapping[str, Any]) -> dict[str, int]:
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, Mapping):
        raise RuntimeError("Gateway metrics response is missing metrics")
    result: dict[str, int] = {}
    for name, value in raw_metrics.items():
        if isinstance(name, str) and type(value) is int:
            result[name] = value
    return result


def _metric_delta(before: Mapping[str, int], after: Mapping[str, int], name: str) -> int:
    return after.get(name, 0) - before.get(name, 0)


def _wait_for_status(
    base_url: str,
    identity: ControlIdentity,
    status_body: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, latest = _post_json(
            base_url,
            STATUS_PATH,
            status_body,
            identity,
            timeout_seconds=min(timeout_seconds, 5.0),
        )
        if status != 200:
            raise RuntimeError("Gateway status request failed")
        hosting_state = latest.get("state")
        if hosting_state == "Running":
            return latest
        if hosting_state in {"Failed", "KickedByUser", "TokenExpired", "Stopped"}:
            raise RuntimeError("Gateway session reached a terminal state before Running")
        time.sleep(0.25)
    raise TimeoutError("Gateway session did not become Running")


def _wait_for_decisions(
    base_url: str,
    identity: ControlIdentity,
    metrics_before: Mapping[str, int],
    *,
    timeout_seconds: float,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, int] = {}
    while time.monotonic() < deadline:
        status, payload = _post_json(
            base_url,
            METRICS_PATH,
            {},
            identity,
            timeout_seconds=min(timeout_seconds, 5.0),
        )
        if status != 200:
            raise RuntimeError("Gateway metrics request failed")
        latest = _metrics(payload)
        if _metric_delta(metrics_before, latest, "llmEventsFailed") != 0:
            raise RuntimeError("Gateway event failure metric increased")
        if _metric_delta(metrics_before, latest, "llmDecisionsRejected") != 0:
            raise RuntimeError("Gateway rejected a v2 decision")
        if _metric_delta(metrics_before, latest, "llmDecisionsAccepted") >= 2:
            return latest
        time.sleep(0.25)
    raise TimeoutError("Gateway did not accept two v2 decisions")


def build_session_evidence(
    *,
    gateway_id: str,
    session_id: str,
    control_generation: int,
    events: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    skill_calls: Sequence[Mapping[str, Any]],
    metrics_before: Mapping[str, int],
    metrics_after: Mapping[str, int],
) -> dict[str, Any]:
    if control_generation <= 0:
        raise ValueError("control_generation must be positive")
    event_ids_by_type: dict[str, list[str]] = {}
    for event in events:
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string")
        event_ids_by_type.setdefault(event_type, []).append(event_id)

    decision_ids = [decision.get("decision_id") for decision in decisions]
    if len(decision_ids) != 2 or any(not isinstance(item, str) or not item for item in decision_ids):
        raise ValueError("exactly two decision IDs are required")
    succeeded_call_ids = [
        call.get("skill_call_id")
        for call in skill_calls
        if call.get("status") == "succeeded"
    ]
    if not succeeded_call_ids or not isinstance(succeeded_call_ids[0], str):
        raise ValueError("a succeeded skill call is required")

    return {
        "sessionId": session_id,
        "gatewayId": gateway_id,
        "controlGeneration": control_generation,
        "eventIdsByType": event_ids_by_type,
        "decisionIds": decision_ids,
        "skillCallIds": [succeeded_call_ids[0]],
        "metricsBefore": dict(metrics_before),
        "metricsAfter": dict(metrics_after),
    }


async def _rows(
    engine: AsyncEngine,
    statement: str,
    parameters: Mapping[str, Any],
) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(sa.text(statement), parameters)
        return [dict(row) for row in result.mappings().all()]


async def _load_session_evidence(
    *,
    gateway_id: str,
    session_id: str,
    metrics_before: Mapping[str, int],
    metrics_after: Mapping[str, int],
    timeout_seconds: float,
) -> dict[str, Any]:
    require_test_database_url()
    engine = await open_verified_test_engine()
    parameters = {"gateway_id": gateway_id, "session_id": session_id}
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            sessions = await _rows(
                engine,
                """
                SELECT current_generation, status
                FROM llm_gateway_sessions
                WHERE gateway_id=:gateway_id AND session_id=:session_id
                """,
                parameters,
            )
            if len(sessions) != 1 or sessions[0]["status"] != "stopped":
                await asyncio.sleep(0.1)
                continue
            generation = int(sessions[0]["current_generation"])
            scoped = {**parameters, "generation": generation}
            events = await _rows(
                engine,
                """
                SELECT event_id, event_type, event_sequence, status
                FROM llm_gateway_events
                WHERE gateway_id=:gateway_id AND session_id=:session_id
                  AND control_generation=:generation
                ORDER BY event_sequence
                """,
                scoped,
            )
            decisions = await _rows(
                engine,
                """
                SELECT decision_id, status
                FROM llm_gateway_decisions
                WHERE gateway_id=:gateway_id AND session_id=:session_id
                  AND control_generation=:generation
                ORDER BY created_at
                """,
                scoped,
            )
            skill_calls = await _rows(
                engine,
                """
                SELECT skill_call_id, status
                FROM llm_gateway_skill_calls
                WHERE gateway_id=:gateway_id AND session_id=:session_id
                ORDER BY created_at
                """,
                parameters,
            )
            required_types = {"session_started", "skill_started", "skill_finished", "session_stopped"}
            if (
                required_types.issubset({str(event["event_type"]) for event in events})
                and all(event["status"] == "succeeded" for event in events)
                and len(decisions) == 2
                and all(decision["status"] == "accepted" for decision in decisions)
                and any(call["status"] == "succeeded" for call in skill_calls)
            ):
                return build_session_evidence(
                    gateway_id=gateway_id,
                    session_id=session_id,
                    control_generation=generation,
                    events=events,
                    decisions=decisions,
                    skill_calls=skill_calls,
                    metrics_before=metrics_before,
                    metrics_after=metrics_after,
                )
            await asyncio.sleep(0.1)
    finally:
        await engine.dispose()
    raise TimeoutError("myAgent2 database did not converge to a complete v2 cycle")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-base-url", required=True)
    parser.add_argument("--gateway-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--account", default="AI1001")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--login-profile-key", default="local-test-login")
    parser.add_argument("--game-profile-key", default="local-test-game")
    parser.add_argument("--server-id", default="0")
    parser.add_argument("--role-id", default="")
    return parser.parse_args()


def _run(args: argparse.Namespace) -> dict[str, Any]:
    identity = ControlIdentity.from_environment()
    metrics_status, metrics_payload = _post_json(
        args.gateway_base_url,
        METRICS_PATH,
        {},
        identity,
        timeout_seconds=5.0,
    )
    if metrics_status != 200:
        raise RuntimeError("Initial Gateway metrics request failed")
    metrics_before = _metrics(metrics_payload)

    login_status, login = _post_json(
        args.gateway_base_url,
        LOGIN_PATH,
        {
            "gatewayId": args.gateway_id,
            "accountGroupId": f"myagent2-v2-e2e-{uuid4().hex[:12]}",
            "account": args.account,
            "loginMode": "account",
            "password": args.password,
            "loginProfileKey": args.login_profile_key,
            "gameProfileKey": args.game_profile_key,
            "serverId": args.server_id,
            "roleId": args.role_id,
        },
        identity,
        timeout_seconds=10.0,
    )
    session_id = login.get("sessionId")
    if login_status != 200 or not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Gateway account-login-start failed")

    status = _wait_for_status(
        args.gateway_base_url,
        identity,
        {"sessionId": session_id, "accountId": "", "roleId": args.role_id},
        timeout_seconds=args.timeout_seconds,
    )
    account_id = status.get("accountId")
    role_id = status.get("roleId")
    if not isinstance(account_id, str) or not isinstance(role_id, str):
        raise RuntimeError("Gateway status response is missing account identity")
    _wait_for_decisions(
        args.gateway_base_url,
        identity,
        metrics_before,
        timeout_seconds=args.timeout_seconds,
    )

    stop_status, stop = _post_json(
        args.gateway_base_url,
        STOP_PATH,
        {
            "sessionId": session_id,
            "accountId": account_id,
            "roleId": role_id,
            "reason": "myagent2-v2-e2e-complete",
        },
        identity,
        timeout_seconds=10.0,
    )
    if stop_status != 200 or stop.get("state") not in {"Stopping", "Stopped"}:
        raise RuntimeError("Gateway stop request failed")

    metrics_after = _wait_for_decisions(
        args.gateway_base_url,
        identity,
        metrics_before,
        timeout_seconds=args.timeout_seconds,
    )
    return asyncio.run(
        _load_session_evidence(
            gateway_id=args.gateway_id,
            session_id=session_id,
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            timeout_seconds=args.timeout_seconds,
        )
    )


def main() -> int:
    args = _parse_args()
    try:
        evidence = _run(args)
        serialized = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
        print(serialized)
    except Exception as error:
        print(json.dumps({"success": False, "category": type(error).__name__}, separators=(",", ":")))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
