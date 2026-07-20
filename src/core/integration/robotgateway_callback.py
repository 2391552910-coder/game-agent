"""RobotGateway 回调客户端。"""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx


class RobotGatewayCallbackSkipped(Exception):  # noqa: N818
    """RobotGateway callback 因未配置而跳过。"""


class RobotGatewayCallbackError(Exception):
    """RobotGateway callback 发送失败。"""


def _json_body_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_llm_gateway_hmac_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    app_id: str,
    app_secret: str,
    request_id: str,
    timestamp_ms: str,
) -> dict[str, str]:
    """构造 LLM Gateway v1 HMAC 请求头。"""
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join([method.upper(), path, timestamp_ms, request_id, body_hash])
    signature = hmac.new(app_secret.encode(), signing_text.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-AppId": app_id,
        "X-TimestampMs": timestamp_ms,
        "X-RequestId": request_id,
        "X-Signature": signature,
    }


def _require_non_blank_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-blank string without surrounding whitespace")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def build_llm_gateway_decision_payload(
    *,
    trace_id: str,
    session_id: str,
    decision_id: str,
    decision_lease_id: str,
    state_version: int,
    recommended_action: dict[str, Any],
) -> dict[str, Any]:
    """将内部决策映射为 /decision 请求体。"""
    action = recommended_action.get("action", "call_skill")
    payload: dict[str, Any] = {
        "traceId": _require_non_blank_string(trace_id, "traceId"),
        "contractVersion": "llm-gateway-http-v1",
        "sessionId": _require_non_blank_string(session_id, "sessionId"),
        "decisionId": decision_id,
        "decisionLeaseId": decision_lease_id,
        "stateVersion": _require_non_negative_int(state_version, "stateVersion"),
        "action": action,
    }

    if action == "call_skill":
        skill_name = _require_non_blank_string(recommended_action.get("skillName"), "skillName")
        schema_version = _require_non_blank_string(recommended_action.get("schemaVersion"), "schemaVersion")
        if "arguments" not in recommended_action or not isinstance(recommended_action["arguments"], dict):
            raise ValueError("call_skill arguments must be a JSON object")
        payload.update({
            "skillName": skill_name,
            "schemaVersion": schema_version,
            "arguments": recommended_action["arguments"],
        })
    elif action == "wait":
        if "arguments" in recommended_action:
            arguments = recommended_action["arguments"]
            if not isinstance(arguments, dict):
                raise ValueError("wait arguments must be a JSON object")
            if set(arguments) - {"waitMs"}:
                raise ValueError("wait arguments only allow waitMs")
            if "waitMs" in arguments and (type(arguments["waitMs"]) is not int or arguments["waitMs"] < 0):
                raise ValueError("waitMs must be a non-negative integer")
            payload["arguments"] = arguments
    elif action == "stop_hosting":
        if "arguments" in recommended_action:
            raise ValueError("stop_hosting must not include arguments")
    else:
        raise ValueError(f"unsupported decision action: {action}")

    return payload


def _validate_llm_gateway_decision_response(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    reason = payload.get("reason")
    if status == "accepted":
        if reason != "ok":
            raise RobotGatewayCallbackError("RobotGateway decision accepted response must include reason=ok")
        skill_call_id = payload.get("skillCallId")
        if skill_call_id is not None and (not isinstance(skill_call_id, str) or not skill_call_id):
            raise RobotGatewayCallbackError("RobotGateway decision skillCallId must be a non-empty string")
        return

    if status == "rejected":
        if reason not in {
            "lease_expired",
            "schema_invalid",
            "skill_not_allowed",
            "state_not_allowed",
            "skill_in_progress",
            "idempotency_key_conflict",
        }:
            raise RobotGatewayCallbackError("RobotGateway decision rejected response has invalid reason")
        if "skillCallId" in payload:
            raise RobotGatewayCallbackError("RobotGateway decision rejected response must not include skillCallId")
        return

    raise RobotGatewayCallbackError("RobotGateway decision response has invalid status")


def build_robotgateway_callback_payload(
    *,
    tenant_id: str,
    user_id: str,
    snapshot: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, Any]:
    """构造发送给 RobotGateway 的分析完成 payload。"""
    return {
        "event_type": "analysis.completed",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "snapshot": snapshot,
        "analysis": output,
    }


def build_robotgateway_callback_headers(api_key: str | None) -> dict[str, str]:
    """构造 RobotGateway callback 请求头。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Callback-API-Key"] = api_key
    return headers


async def send_robotgateway_analysis_callback(
    *,
    callback_url: str | None,
    api_key: str | None,
    timeout_seconds: float,
    tenant_id: str,
    user_id: str,
    snapshot: dict[str, Any],
    output: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """向 RobotGateway 发送玩家分析完成回调。"""
    if not callback_url:
        raise RobotGatewayCallbackSkipped("RobotGateway callback URL is not configured")

    payload = build_robotgateway_callback_payload(
        tenant_id=tenant_id,
        user_id=user_id,
        snapshot=snapshot,
        output=output,
    )
    headers = build_robotgateway_callback_headers(api_key)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.post(callback_url, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RobotGatewayCallbackError(str(exc)) from exc


async def send_llm_gateway_decision(
    *,
    decision_url: str,
    app_id: str,
    app_secret: str,
    timeout_seconds: float,
    trace_id: str,
    session_id: str,
    decision_id: str,
    decision_lease_id: str,
    state_version: int,
    recommended_action: dict[str, Any],
    request_id: str | None = None,
    timestamp_ms: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """向 RobotGateway v1 /decision 提交一次 call_skill 决策。"""
    payload = build_llm_gateway_decision_payload(
        trace_id=trace_id,
        session_id=session_id,
        decision_id=decision_id,
        decision_lease_id=decision_lease_id,
        state_version=state_version,
        recommended_action=recommended_action,
    )
    body = _json_body_bytes(payload)
    request_id = request_id or f"req-{uuid4().hex}"
    timestamp_ms = timestamp_ms or str(int(datetime.now(UTC).timestamp() * 1000))
    path = httpx.URL(decision_url).raw_path.decode()
    headers = build_llm_gateway_hmac_headers(
        method="POST",
        path=path,
        body=body,
        app_id=app_id,
        app_secret=app_secret,
        request_id=request_id,
        timestamp_ms=timestamp_ms,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.post(decision_url, content=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise RobotGatewayCallbackError(str(exc)) from exc
    except ValueError as exc:
        raise RobotGatewayCallbackError("RobotGateway decision response is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise RobotGatewayCallbackError("RobotGateway decision response must be a JSON object")
    _validate_llm_gateway_decision_response(payload)
    return payload
