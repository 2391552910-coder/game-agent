from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr, ValidationError

from src.core.integration.llm_gateway_v2.auth import build_outbound_hmac_headers
from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_decision_response

DecisionResponseStatus = Literal["accepted", "rejected"]
MAX_RESPONSE_BODY_PREVIEW = 4_096


def _response_body_preview(content: bytes) -> str:
    return content[:MAX_RESPONSE_BODY_PREVIEW].decode("utf-8", errors="replace")


class DecisionClientTransportError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("gateway v2 decision transport failed")


class DecisionClientProtocolError(Exception):
    def __init__(
        self,
        category: str,
        *,
        http_status: int | None = None,
        response_body_text: str | None = None,
    ) -> None:
        self.category = category
        self.http_status = http_status
        self.response_body_text = response_body_text
        super().__init__("gateway v2 decision response invalid")


@dataclass(frozen=True)
class DecisionClientResult:
    http_status: int
    status: DecisionResponseStatus
    reason: str
    skill_call_id: str | None
    trace_id: str | None = None
    session_id: str | None = None
    decision_id: str | None = None
    decision_lease_id: str | None = None
    control_generation: int | None = None
    state_version: int | None = None
    response_body_json: Mapping[str, Any] | None = None

    @property
    def is_idempotency_conflict(self) -> bool:
        return self.status == "rejected" and self.reason == "idempotency_key_conflict"


def validate_decision_response(
    request_action: str,
    http_status: int,
    payload: object,
    *,
    request_identity: Mapping[str, Any] | None = None,
) -> DecisionClientResult:
    try:
        response = parse_gateway_v2_decision_response(payload)
    except ValidationError:
        raise DecisionClientProtocolError(
            "response_schema_invalid",
            http_status=http_status,
        ) from None

    if request_identity is not None:
        _validate_response_identity(response, request_identity)

    decision_lease_id = response.decision_lease_id
    if response.status == "accepted" and decision_lease_id is None:
        request_lease_id = None if request_identity is None else request_identity.get("decisionLeaseId")
        if not isinstance(request_lease_id, str) or not request_lease_id:
            raise DecisionClientProtocolError("response_identity_missing")
        decision_lease_id = request_lease_id

    result_identity = {
        "trace_id": response.trace_id,
        "session_id": response.session_id,
        "decision_id": response.decision_id,
        "decision_lease_id": decision_lease_id,
        "control_generation": response.control_generation,
        "state_version": response.state_version,
    }

    if response.status == "rejected":
        return DecisionClientResult(
            http_status=http_status,
            status="rejected",
            reason=response.reason,
            skill_call_id=None,
            response_body_json=dict(payload) if isinstance(payload, Mapping) else None,
            **result_identity,
        )

    if not 200 <= http_status < 300:
        raise DecisionClientProtocolError(
            "accepted_http_status_invalid",
            http_status=http_status,
        )

    skill_call_id = response.skill_call_id
    if request_action in {"call_skill", "stop_hosting"}:
        if skill_call_id is None:
            raise DecisionClientProtocolError(
                "accepted_skill_call_id_missing",
                http_status=http_status,
            )
    elif request_action in {"wait", "no_op"}:
        if skill_call_id is not None:
            raise DecisionClientProtocolError(
                "accepted_skill_call_id_unexpected",
                http_status=http_status,
            )
    else:
        raise DecisionClientProtocolError(
            "request_action_invalid",
            http_status=http_status,
        )

    return DecisionClientResult(
        http_status=http_status,
        status="accepted",
        reason=response.reason,
        skill_call_id=skill_call_id,
        response_body_json=dict(payload) if isinstance(payload, Mapping) else None,
        **result_identity,
    )


def _validate_response_identity(response: Any, expected: Mapping[str, Any]) -> None:
    fields = (
        ("traceId", "trace_id"),
        ("sessionId", "session_id"),
        ("decisionId", "decision_id"),
        ("decisionLeaseId", "decision_lease_id"),
        ("controlGeneration", "control_generation"),
        ("stateVersion", "state_version"),
    )
    for request_key, response_attribute in fields:
        expected_value = expected.get(request_key)
        actual_value = getattr(response, response_attribute)
        if actual_value is None:
            if response.status == "accepted" and request_key != "decisionLeaseId":
                raise DecisionClientProtocolError("response_identity_missing")
            continue
        if actual_value != expected_value:
            raise DecisionClientProtocolError("response_identity_mismatch")


def _request_identity(raw_body: bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw_body)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    required = (
        "traceId",
        "sessionId",
        "decisionId",
        "decisionLeaseId",
        "controlGeneration",
        "stateVersion",
    )
    if not all(key in decoded for key in required):
        return None
    return {key: decoded[key] for key in required}


class GatewayV2DecisionClient:
    def __init__(
        self,
        *,
        decision_url: str,
        app_id: str,
        app_secret: SecretStr,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        request_id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not decision_url.strip():
            raise ValueError("decision_url must not be empty")
        if not app_id.strip():
            raise ValueError("app_id must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._decision_url = decision_url
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._request_id_factory = request_id_factory or (lambda: str(uuid4()))
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))

    async def send(self, *, action: str, raw_body: bytes) -> DecisionClientResult:
        if type(raw_body) is not bytes:
            raise TypeError("raw_body must be bytes")
        path = httpx.URL(self._decision_url).raw_path.decode("ascii")
        headers = build_outbound_hmac_headers(
            method="POST",
            path=path,
            raw_body=raw_body,
            app_id=self._app_id,
            app_secret=self._app_secret,
            request_id=self._request_id_factory(),
            timestamp_ms=self._now_ms(),
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._decision_url,
                    content=raw_body,
                    headers=headers,
                )
        except httpx.TimeoutException:
            raise DecisionClientTransportError("timeout") from None
        except httpx.RequestError:
            raise DecisionClientTransportError("request_failed") from None

        try:
            payload = response.json()
        except ValueError:
            raise DecisionClientProtocolError(
                "response_not_json",
                http_status=response.status_code,
                response_body_text=_response_body_preview(response.content),
            ) from None
        try:
            return validate_decision_response(
                action,
                response.status_code,
                payload,
                request_identity=_request_identity(raw_body),
            )
        except DecisionClientProtocolError as error:
            if error.response_body_text is not None:
                raise
            raise DecisionClientProtocolError(
                error.category,
                http_status=error.http_status,
                response_body_text=_response_body_preview(response.content),
            ) from None
