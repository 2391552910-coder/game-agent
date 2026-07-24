from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.config import settings
from src.core.integration.llm_gateway_v2.auth import (
    GatewayAuthError,
    InboundGatewayIdentity,
    resolve_inbound_identity,
    verify_inbound_hmac,
)
from src.core.integration.llm_gateway_v2.contracts import (
    GatewayV2BatchAck,
    GatewayV2BatchEnvelope,
    GatewayV2Capabilities,
    GatewayV2Error,
    GatewayV2ErrorDetail,
    build_gateway_v2_capabilities,
)
from src.core.integration.llm_gateway_v2.event_service import (
    EventBatchInvalid,
    EventContentConflict,
    EventService,
    EventServiceUnavailable,
    build_gateway_v2_event_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gateway/v2", tags=["gateway-v2"])

_AUTH_MESSAGES = {
    "auth_header_invalid": "authentication headers invalid",
    "auth_timestamp_invalid": "authentication timestamp invalid",
    "signature_invalid": "request signature invalid",
    "app_id_unknown": "application not authorized",
    "gateway_not_authorized": "gateway not authorized",
    "tenant_not_configured": "gateway tenant not configured",
}
_REQUEST_SCHEMA = GatewayV2BatchEnvelope.model_json_schema()
_HMAC_PARAMETERS = [
    {
        "name": name,
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
    }
    for name in ("X-AppId", "X-TimestampMs", "X-RequestId", "X-Signature")
]


def get_gateway_v2_event_service() -> EventService:
    return build_gateway_v2_event_service(settings.llm_gateway_v2_max_event_batch_size)


async def accept_gateway_event_batch(
    identity: InboundGatewayIdentity,
    envelope: GatewayV2BatchEnvelope,
) -> GatewayV2BatchAck:
    return await get_gateway_v2_event_service().accept_event_batch(identity, envelope)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    body = GatewayV2Error(error=GatewayV2ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


@router.get(
    "/capabilities",
    response_model=GatewayV2Capabilities,
    responses={503: {"model": GatewayV2Error}},
)
async def capabilities(request: Request) -> GatewayV2Capabilities | JSONResponse:
    if not settings.llm_gateway_v2_enabled:
        return _error(503, "service_disabled", "service disabled")

    readiness = await request.app.state.readiness_service.snapshot()
    if readiness.status != "ready":
        return _error(503, "service_unavailable", "service unavailable")

    return build_gateway_v2_capabilities(
        max_event_batch_size=settings.llm_gateway_v2_max_event_batch_size,
        max_decision_ttl_ms=settings.llm_gateway_v2_max_decision_ttl_ms,
    )


@router.post(
    "/events",
    response_model=GatewayV2BatchAck,
    responses={
        400: {"model": GatewayV2Error},
        401: {"model": GatewayV2Error},
        409: {"model": GatewayV2Error},
        500: {"model": GatewayV2Error},
        503: {"model": GatewayV2Error},
    },
    openapi_extra={
        "parameters": _HMAC_PARAMETERS,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _REQUEST_SCHEMA}},
        },
    },
)
async def receive_events(
    request: Request,
) -> GatewayV2BatchAck | JSONResponse:
    raw_body = await request.body()
    try:
        app_id = verify_inbound_hmac(
            request.method,
            request.url.path,
            raw_body,
            request.headers,
            int(time.time() * 1000),
        )
    except GatewayAuthError as error:
        return _error(error.http_status, error.code, _AUTH_MESSAGES[error.code])

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _error(400, "bad_request", "bad request")

    try:
        decoded: Any = json.loads(raw_body)
        envelope = GatewayV2BatchEnvelope.model_validate(decoded)
        identity = resolve_inbound_identity(app_id, envelope.gateway_id)
    except GatewayAuthError as error:
        return _error(error.http_status, error.code, _AUTH_MESSAGES[error.code])
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
        return _error(400, "bad_request", "bad request")

    max_batch_size = settings.llm_gateway_v2_max_event_batch_size
    if len(envelope.events) > max_batch_size:
        return _error(400, "bad_request", "bad request")
    if not settings.llm_gateway_v2_enabled:
        return _error(503, "service_disabled", "service disabled")

    try:
        return await accept_gateway_event_batch(identity, envelope)
    except EventBatchInvalid:
        return _error(400, "bad_request", "bad request")
    except EventContentConflict:
        return _error(409, "event_content_conflict", "event content conflicts with stored event")
    except EventServiceUnavailable:
        return _error(503, "service_unavailable", "service unavailable")
    except Exception:
        logger.error("LLM Gateway v2 event admission failed, trace_id=%s", envelope.trace_id)
        return _error(500, "internal_error", "internal error")
