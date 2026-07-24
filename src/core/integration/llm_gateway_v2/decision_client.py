from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr, ValidationError

from src.core.integration.llm_gateway_v2.auth import build_outbound_hmac_headers
from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_decision_response

DecisionResponseStatus = Literal["accepted", "rejected"]


class DecisionClientTransportError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("gateway v2 decision transport failed")


class DecisionClientProtocolError(Exception):
    def __init__(self, category: str, *, http_status: int | None = None) -> None:
        self.category = category
        self.http_status = http_status
        super().__init__("gateway v2 decision response invalid")


@dataclass(frozen=True)
class DecisionClientResult:
    http_status: int
    status: DecisionResponseStatus
    reason: str
    skill_call_id: str | None

    @property
    def is_idempotency_conflict(self) -> bool:
        return self.status == "rejected" and self.reason == "idempotency_key_conflict"


def validate_decision_response(
    request_action: str,
    http_status: int,
    payload: object,
) -> DecisionClientResult:
    try:
        response = parse_gateway_v2_decision_response(payload)
    except ValidationError:
        raise DecisionClientProtocolError(
            "response_schema_invalid",
            http_status=http_status,
        ) from None

    if response.status == "rejected":
        return DecisionClientResult(
            http_status=http_status,
            status="rejected",
            reason=response.reason,
            skill_call_id=None,
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
    )


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
            ) from None
        return validate_decision_response(action, response.status_code, payload)
