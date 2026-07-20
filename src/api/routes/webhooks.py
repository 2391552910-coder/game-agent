"""
游戏服务器 Webhook 端点。

接收玩家在线/离线事件，触发或取消分析流程。
接收玩家在线期间的行为事件，写入 session_events 表。
"""

import json
import logging
import math
import re
import time
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from src.core.integration.robotgateway_callback import build_llm_gateway_hmac_headers, send_llm_gateway_decision

logger = logging.getLogger(__name__)

router = APIRouter()
gateway_router = APIRouter()

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _settings():
    from src.config import settings

    return settings


class GatewayPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    z: float

    @field_validator("x", "y", "z", mode="before")
    @classmethod
    def validate_finite_json_number(cls, value: Any) -> Any:
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise ValueError("position 坐标必须是有限 JSON number")
        return value


class GatewaySessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: StrictStr = Field(alias="sessionId")
    account_id: StrictStr = Field(alias="accountId")
    role_id: StrictStr = Field(alias="roleId")
    scene_id: StrictInt = Field(alias="sceneId")
    state: Literal["Running", "Stopped", "Failed"]
    position: GatewayPosition
    controllable: StrictBool


class GatewaySkillPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_call_id: StrictStr = Field(alias="skillCallId")
    skill_name: StrictStr = Field(alias="skillName")
    reason: StrictStr


class GatewayStopPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "admin_stop",
        "stop_hosting_requested",
        "player_online",
        "server_kicked",
        "gateway_shutdown",
        "runtime_error",
    ]


class GatewayObservationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["wait_completed", "state_changed"]


class GatewayEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: GatewaySessionPayload
    skill: GatewaySkillPayload | None = None
    stop: GatewayStopPayload | None = None
    observation: GatewayObservationPayload | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_blocks(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for block_name in ("skill", "stop", "observation"):
                if block_name in data and data[block_name] is None:
                    raise ValueError(f"{block_name} block 不允许显式 null")
        return data


class GatewayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: StrictStr = Field(alias="eventId")
    event_type: Literal["session_started", "skill_finished", "session_stopped", "observation_updated"] = Field(
        alias="eventType"
    )
    session_id: StrictStr | None = Field(default=None, alias="sessionId")
    state_version: StrictInt | None = Field(default=None, alias="stateVersion")
    decision_lease_id: StrictStr | None = Field(default=None, alias="decisionLeaseId")
    occurred_at_ms: StrictInt = Field(alias="occurredAtMs")
    payload: GatewayEventPayload

    @model_validator(mode="before")
    @classmethod
    def reject_null_decision_lease_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("decisionLeaseId", "") is None:
            raise ValueError("decisionLeaseId 不允许显式 null")
        return data

    @model_validator(mode="after")
    def validate_event_shape(self) -> "GatewayEvent":
        if self.event_type == "session_stopped":
            if self.decision_lease_id is not None or "decision_lease_id" in self.model_fields_set:
                raise ValueError("session_stopped 不允许 decisionLeaseId")
            if self.payload.stop is None or self.payload.skill is not None or self.payload.observation is not None:
                raise ValueError("session_stopped 只能携带 session + stop")
            if self.payload.session.state not in {"Stopped", "Failed"} or self.payload.session.controllable:
                raise ValueError("session_stopped 必须是终态且不可控")
            return self

        if not self.decision_lease_id:
            raise ValueError(f"{self.event_type} 必须携带 decisionLeaseId")
        if self.payload.session.state != "Running" or not self.payload.session.controllable:
            raise ValueError(f"{self.event_type} 必须是 Running 且 controllable=true")

        if self.event_type == "session_started":
            if self.payload.skill is not None or self.payload.stop is not None or self.payload.observation is not None:
                raise ValueError("session_started 只能携带 session")
        elif self.event_type == "skill_finished":
            if self.payload.skill is None or self.payload.stop is not None or self.payload.observation is not None:
                raise ValueError("skill_finished 只能携带 session + skill")
        elif (
            self.event_type == "observation_updated"
            and (self.payload.observation is None or self.payload.skill is not None or self.payload.stop is not None)
        ):
            raise ValueError("observation_updated 只能携带 session + observation")
        return self


class GatewayEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: StrictStr = Field(alias="traceId")
    gateway_id: StrictStr = Field(alias="gatewayId")
    contract_version: Literal["llm-gateway-http-v1"] = Field(alias="contractVersion")
    event: GatewayEvent


class GatewayEventBatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: StrictStr = Field(alias="traceId")
    gateway_id: StrictStr = Field(alias="gatewayId")
    contract_version: Literal["llm-gateway-http-v1"] = Field(alias="contractVersion")
    sent_at_ms: StrictInt | None = Field(default=None, alias="sentAtMs")
    events: list[GatewayEvent]

    @field_validator("events")
    @classmethod
    def validate_non_empty_events(cls, value: list[GatewayEvent]) -> list[GatewayEvent]:
        if not value:
            raise ValueError("events 不能为空")
        return value


def _protocol_error(code: str, status_code: int) -> JSONResponse:
    messages = {
        "bad_request": "bad request",
        "signature_invalid": "request signature invalid",
        "timestamp_expired": "request timestamp expired",
        "internal_error": "internal error",
    }
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": messages[code]}})


def _valid_id(value: str | None) -> bool:
    return isinstance(value, str) and bool(_ID_PATTERN.fullmatch(value))


def _app_secret_for(app_id: str) -> str | None:
    app_secrets = getattr(_settings(), "llm_gateway_app_secrets", {}) or {}
    if not isinstance(app_secrets, dict):
        return None
    secret = app_secrets.get(app_id)
    return secret if isinstance(secret, str) and secret else None


def _validate_hmac_headers(request: Request, raw_body: bytes) -> JSONResponse | None:
    app_id = request.headers.get("X-AppId")
    timestamp_ms = request.headers.get("X-TimestampMs")
    request_id = request.headers.get("X-RequestId")
    signature = request.headers.get("X-Signature")

    if not _valid_id(app_id) or not _valid_id(request_id):
        return _protocol_error("signature_invalid", 401)
    if not isinstance(timestamp_ms, str) or not timestamp_ms.isdigit():
        return _protocol_error("timestamp_expired", 401)
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return _protocol_error("signature_invalid", 401)

    tolerance_ms = int(getattr(_settings(), "llm_gateway_timestamp_tolerance_ms", 300_000))
    now_ms = int(time.time() * 1000)
    if abs(now_ms - int(timestamp_ms)) > tolerance_ms:
        return _protocol_error("timestamp_expired", 401)

    app_secret = _app_secret_for(app_id)
    if app_secret is None:
        return _protocol_error("signature_invalid", 401)

    expected = build_llm_gateway_hmac_headers(
        method=request.method,
        path=request.url.path,
        body=raw_body,
        app_id=app_id,
        app_secret=app_secret,
        request_id=request_id,
        timestamp_ms=timestamp_ms,
    )["X-Signature"]
    if signature != expected:
        return _protocol_error("signature_invalid", 401)
    return None


async def _claim_gateway_event_idempotency(event_id: str, body_sha256: str) -> str:
    from src.core.infrastructure.redis import get_redis

    redis = await get_redis()
    key = f"llm-gateway:event:{event_id}"
    existing = await redis.get(key)
    if isinstance(existing, bytes):
        existing = existing.decode()
    if existing is None:
        ttl = int(getattr(_settings(), "llm_gateway_idempotency_ttl_seconds", 86_400))
        claimed = await redis.set(key, body_sha256, ex=ttl, nx=True)
        if claimed:
            return "accepted"
        existing = await redis.get(key)
        if isinstance(existing, bytes):
            existing = existing.decode()
        if existing == body_sha256:
            return "duplicate"
        return "conflict"
    if existing == body_sha256:
        return "duplicate"
    return "conflict"


def _has_query_params(request: Request) -> bool:
    return bool(request.url.query)


async def _validate_gateway_event_request(request: Request) -> bytes | JSONResponse:
    if _has_query_params(request):
        return _protocol_error("bad_request", 400)

    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        return _protocol_error("bad_request", 400)

    raw_body = await request.body()
    hmac_error = _validate_hmac_headers(request, raw_body)
    if hmac_error is not None:
        return hmac_error
    return raw_body


def _parse_gateway_event_envelope(raw_body: bytes) -> GatewayEventEnvelope | None:
    try:
        payload = json.loads(raw_body)
        return GatewayEventEnvelope.model_validate(payload)
    except Exception:
        return None


def _parse_gateway_event_batch_envelope(raw_body: bytes) -> GatewayEventBatchEnvelope | None:
    try:
        payload = json.loads(raw_body)
        return GatewayEventBatchEnvelope.model_validate(payload)
    except Exception:
        return None


def _envelope_from_batch_event(batch: GatewayEventBatchEnvelope, event: GatewayEvent) -> GatewayEventEnvelope:
    return GatewayEventEnvelope(
        traceId=batch.trace_id,
        gatewayId=batch.gateway_id,
        contractVersion=batch.contract_version,
        event=event,
    )


def _json_body_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _single_event_body_hash(envelope: GatewayEventEnvelope) -> str:
    payload = envelope.model_dump(mode="json", by_alias=True)
    return sha256(_json_body_bytes(payload)).hexdigest()


def _gateway_event_ids_are_valid(envelope: GatewayEventEnvelope) -> bool:
    values = (
        envelope.trace_id,
        envelope.gateway_id,
        envelope.event.event_id,
        envelope.event.session_id,
        envelope.event.decision_lease_id,
        envelope.event.payload.session.session_id,
        envelope.event.payload.session.account_id,
        envelope.event.payload.session.role_id,
    )
    return all(value is None or _valid_id(value) for value in values)


def _gateway_event_idempotency_response(event_id: str, status: str) -> JSONResponse | dict[str, str]:
    if status == "conflict":
        return _protocol_error("bad_request", 400)
    return {"status": status, "eventId": event_id}


def _gateway_batch_response(trace_id: str, results: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "status": "accepted",
        "traceId": trace_id,
        "results": results,
    }


class PlayerEvent(BaseModel):
    user_id: str = Field(..., description="玩家 ID")
    event_type: str = Field(..., description="事件类型: online / offline / behavior_checkpoint")
    timestamp: float = Field(..., description="事件时间戳")
    snapshot: dict | None = Field(default=None, description="玩家快照数据（可选）")
    # behavior_checkpoint 专用字段
    session_id: str | None = Field(default=None, description="会话 ID（behavior_checkpoint 必填）")
    behavior_event: dict | None = Field(default=None, description="行为事件详情")


@gateway_router.post("/events")
async def handle_gateway_event(request: Request):
    """接收 LLM Gateway v1 runtime 事件。"""
    raw_body_or_error = await _validate_gateway_event_request(request)
    if isinstance(raw_body_or_error, JSONResponse):
        return raw_body_or_error
    raw_body = raw_body_or_error

    envelope = _parse_gateway_event_envelope(raw_body)
    if envelope is not None:
        return await _handle_single_gateway_event_envelope(envelope, sha256(raw_body).hexdigest())

    batch = _parse_gateway_event_batch_envelope(raw_body)
    if batch is None:
        return _protocol_error("bad_request", 400)

    envelopes = [_envelope_from_batch_event(batch, event) for event in batch.events]
    if len(envelopes) == 1:
        return await _handle_single_gateway_event_envelope(envelopes[0], _single_event_body_hash(envelopes[0]))

    results: list[dict[str, str]] = []
    for item in envelopes:
        response = await _handle_single_gateway_event_envelope(item, _single_event_body_hash(item))
        if isinstance(response, JSONResponse):
            return response
        results.append(response)
    return _gateway_batch_response(batch.trace_id, results)


async def _handle_single_gateway_event_envelope(
    envelope: GatewayEventEnvelope,
    body_hash: str,
) -> JSONResponse | dict[str, str]:
    if not _gateway_event_ids_are_valid(envelope):
        return _protocol_error("bad_request", 400)

    idempotency_status = await _claim_gateway_event_idempotency(envelope.event.event_id, body_hash)
    response = _gateway_event_idempotency_response(envelope.event.event_id, idempotency_status)
    if isinstance(response, JSONResponse):
        return response

    if idempotency_status == "accepted":
        await _handle_gateway_event_business(envelope)

    return response


@router.post("/player-event")
async def handle_player_event(event: PlayerEvent, request: Request):
    """处理玩家在线/离线/行为事件。"""
    tenant_id = request.state.tenant_id

    if event.event_type == "offline":
        from src.core.scheduler.triggers import schedule_offline_analysis

        run_id = await schedule_offline_analysis(
            user_id=event.user_id,
            tenant_id=tenant_id,
            snapshot=event.snapshot,
        )
        if run_id is None:
            return {"status": "debounced", "user_id": event.user_id}
        return {"status": "scheduled", "user_id": event.user_id, "flow_run_id": run_id}

    elif event.event_type == "online":
        from src.core.scheduler.triggers import cancel_offline_analysis

        await cancel_offline_analysis(user_id=event.user_id)
        return {"status": "cancelled", "user_id": event.user_id}

    elif event.event_type == "behavior_checkpoint":
        if not event.session_id:
            raise HTTPException(status_code=422, detail="behavior_checkpoint 事件必须提供 session_id")

        await _write_behavior_event(
            tenant_id=tenant_id,
            user_id=event.user_id,
            session_id=event.session_id,
            behavior_event=event.behavior_event or {},
            snapshot=event.snapshot,
        )
        return {"status": "recorded", "user_id": event.user_id}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知事件类型: {event.event_type}",
        )


async def _handle_gateway_event_business(envelope: GatewayEventEnvelope) -> None:
    """v1 事件业务接管点。

    首版先保证事件可靠接收和幂等；session_stopped 仅记录，带 lease 的事件后续接入 Agent 决策。
    """
    logger.info(
        "[gateway_v1] event accepted, trace_id=%s, event_id=%s, event_type=%s, session_id=%s",
        envelope.trace_id,
        envelope.event.event_id,
        envelope.event.event_type,
        envelope.event.payload.session.session_id,
    )
    if envelope.event.decision_lease_id is None or envelope.event.event_type == "session_stopped":
        return

    current_settings = _settings()
    decision_url = getattr(current_settings, "llm_gateway_decision_url", None)
    decision_app_id = getattr(current_settings, "llm_gateway_decision_app_id", None)
    decision_app_secret = getattr(current_settings, "llm_gateway_decision_app_secret", None)
    if not decision_url or not decision_app_id or not decision_app_secret:
        logger.warning("[gateway_v1] /decision 未配置，跳过 Agent 和决策发送, event_id=%s", envelope.event.event_id)
        return

    output = await run_gateway_v1_agent(
        tenant_id=_tenant_id_for_gateway_event(envelope),
        trace_id=envelope.trace_id,
        event_id=envelope.event.event_id,
        event_type=envelope.event.event_type,
        session_id=envelope.event.session_id or envelope.event.payload.session.session_id,
        state_version=envelope.event.state_version if envelope.event.state_version is not None else 0,
        decision_lease_id=envelope.event.decision_lease_id,
        session=envelope.event.payload.session.model_dump(mode="json", by_alias=True),
    )
    actions = output.get("recommended_actions") if isinstance(output, dict) else None
    if not actions:
        logger.warning("[gateway_v1] Agent 未返回 recommended_actions, event_id=%s", envelope.event.event_id)
        return

    await send_llm_gateway_decision(
        decision_url=decision_url,
        app_id=decision_app_id,
        app_secret=decision_app_secret,
        timeout_seconds=float(getattr(current_settings, "llm_gateway_decision_timeout_seconds", 10.0)),
        trace_id=envelope.trace_id,
        session_id=envelope.event.session_id or envelope.event.payload.session.session_id,
        decision_id=f"decision-{uuid4().hex}",
        decision_lease_id=envelope.event.decision_lease_id,
        state_version=envelope.event.state_version if envelope.event.state_version is not None else 0,
        recommended_action=actions[0],
    )


def _tenant_id_for_gateway_event(envelope: GatewayEventEnvelope) -> str:
    app_tenants = getattr(_settings(), "llm_gateway_app_tenants", {}) or {}
    if isinstance(app_tenants, dict):
        tenant_id = app_tenants.get(envelope.gateway_id)
        if isinstance(tenant_id, str) and tenant_id:
            return tenant_id
    return envelope.gateway_id


async def run_gateway_v1_agent(
    *,
    tenant_id: str,
    trace_id: str,
    event_id: str,
    event_type: str,
    session_id: str,
    state_version: int,
    decision_lease_id: str,
    session: dict,
) -> dict:
    """用 v1 Gateway event 触发现有 LangGraph Agent。"""
    user_id = session.get("roleId") or session.get("accountId") or session["sessionId"]
    snapshot = {
        "user_id": user_id,
        "traceId": trace_id,
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": session_id,
        "stateVersion": state_version,
        "decisionLeaseId": decision_lease_id,
        "session": session,
        "position": session.get("position"),
        "sceneId": session.get("sceneId"),
        "accountId": session.get("accountId"),
        "roleId": session.get("roleId"),
    }
    from src.core.agents.orchestrator import build_orchestrator

    graph = build_orchestrator().compile()
    result = await graph.ainvoke(
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "snapshot": snapshot,
            "rag_context": "",
            "enriched_context": "",
            "behavior_report": "",
            "reasoned_actions": [],
            "final_output": {},
            "errors": [],
            "tracking_summary": "",
            "anomalies": [],
            "abandoned_tracking_ids": [],
        }
    )
    output = result.get("final_output", {})
    if not output:
        errors = result.get("errors", [])
        error_text = "; ".join(str(error) for error in errors) if errors else "final_output为空"
        raise RuntimeError(f"Gateway v1 Agent 分析失败: {error_text}")
    return output


async def _write_behavior_event(
    tenant_id: str,
    user_id: str,
    session_id: str,
    behavior_event: dict,
    snapshot: dict | None,
) -> None:
    """将行为事件写入 session_events 表。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    event_type = behavior_event.get("type", "unknown")
    event_data = behavior_event.get("data")

    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO session_events (
                    tenant_id, user_id, session_id,
                    event_type, event_data, snapshot
                ) VALUES (
                    :tenant_id, :user_id, :session_id,
                    :event_type, :event_data, :snapshot
                )
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": session_id,
                "event_type": event_type,
                "event_data": event_data,
                "snapshot": snapshot,
            },
        )
    logger.debug(
        "[webhook] 行为事件已写入, user_id=%s, session_id=%s, type=%s",
        user_id,
        session_id,
        event_type,
    )
