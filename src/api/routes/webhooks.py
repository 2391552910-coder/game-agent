"""
游戏服务器 Webhook 端点。

接收玩家在线/离线事件，触发或取消分析流程。
接收玩家在线期间的行为事件，写入 session_events 表。
"""

import hmac
import json
import logging
import math
import re
import time
from hashlib import sha256
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from src.core.integration.gateway_event_queue import enqueue_gateway_event
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields
from src.core.integration.robotgateway_callback import build_llm_gateway_hmac_headers, send_llm_gateway_decision

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)
router = APIRouter(dependencies=[Security(_api_key_header)])
gateway_router = APIRouter()

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _settings():
    from src.config import settings

    return settings


class GatewayPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(validation_alias=AliasChoices("x", "X"))
    y: float = Field(validation_alias=AliasChoices("y", "Y"))
    z: float = Field(validation_alias=AliasChoices("z", "Z"))

    @field_validator("x", "y", "z", mode="before")
    @classmethod
    def validate_finite_json_number(cls, value: Any) -> Any:
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise ValueError("position 坐标必须是有限 JSON number")
        return value


class GatewaySessionPayload(BaseModel):
    # Gateway 的 RobotSessionSnapshot 会随运行时增加投影字段，不能使用 extra="forbid"。
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    session_id: StrictStr = Field(
        validation_alias=AliasChoices("sessionId", "SessionId"),
        serialization_alias="sessionId",
    )
    account_id: StrictStr = Field(
        validation_alias=AliasChoices("accountId", "AccountId"),
        serialization_alias="accountId",
    )
    role_id: StrictStr = Field(
        validation_alias=AliasChoices("roleId", "RoleId"),
        serialization_alias="roleId",
    )
    scene_id: StrictInt | None = Field(
        default=None,
        validation_alias=AliasChoices("sceneId", "SceneId"),
        serialization_alias="sceneId",
    )
    state: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("state", "State"),
        serialization_alias="state",
    )
    position: GatewayPosition | None = Field(default=None, validation_alias=AliasChoices("position", "Position"))
    controllable: StrictBool | None = Field(
        default=None,
        validation_alias=AliasChoices("controllable", "Controllable"),
        serialization_alias="controllable",
    )

    @model_validator(mode="after")
    def validate_identity(self) -> "GatewaySessionPayload":
        for name, value in (
            ("sessionId", self.session_id),
            ("accountId", self.account_id),
            ("roleId", self.role_id),
        ):
            if not _valid_id(value):
                raise ValueError(f"{name} 格式无效")
        return self


class GatewayEventPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event_type: Literal["observation_updated", "decision_rejected", "skill_finished"] = Field(alias="eventType")
    reason: StrictStr | None = None
    session: GatewaySessionPayload
    available_skills: list[dict[str, Any]] = Field(default_factory=list, alias="availableSkills")
    skill_argument_hints: list[dict[str, Any]] = Field(default_factory=list, alias="skillArgumentHints")
    last_skill_result: dict[str, Any] | None = Field(default=None, alias="lastSkillResult")


class GatewayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: StrictStr = Field(alias="eventId")
    event_type: Literal["observation_updated", "decision_rejected", "skill_finished"] = Field(alias="eventType")
    session_id: StrictStr = Field(alias="sessionId")
    state_version: StrictInt = Field(alias="stateVersion", ge=0)
    decision_lease_id: StrictStr = Field(alias="decisionLeaseId")
    occurred_at_ms: StrictInt = Field(alias="occurredAtMs")
    payload: GatewayEventPayload

    @model_validator(mode="after")
    def validate_event_shape(self) -> "GatewayEvent":
        if not _valid_id(self.event_id) or not _valid_id(self.session_id) or not _valid_id(self.decision_lease_id):
            raise ValueError("Gateway event ID 格式无效")
        if self.payload.event_type != self.event_type:
            raise ValueError("payload.eventType 必须与 eventType 一致")
        if self.payload.session.session_id != self.session_id:
            raise ValueError("event.sessionId 必须与 payload.session.sessionId 一致")
        return self


class GatewayEventBatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: StrictStr = Field(alias="traceId")
    gateway_id: StrictStr = Field(alias="gatewayId")
    contract_version: Literal["llm-gateway-http-v1"] = Field(alias="contractVersion")
    sent_at_ms: StrictInt | None = Field(default=None, alias="sentAtMs")
    events: list[GatewayEvent] = Field(min_length=1)


_GATEWAY_EVENTS_EXAMPLE = {
    "traceId": "trace-001",
    "gatewayId": "gateway-01",
    "contractVersion": "llm-gateway-http-v1",
    "sentAtMs": 1750000000000,
    "events": [
        {
            "eventId": "evt-001",
            "eventType": "observation_updated",
            "sessionId": "session-001",
            "stateVersion": 1,
            "decisionLeaseId": "lease-001",
            "occurredAtMs": 1750000000001,
            "payload": {
                "eventType": "observation_updated",
                "reason": "state_changed",
                "session": {
                    "sessionId": "session-001",
                    "accountId": "account-001",
                    "roleId": "role-001",
                    "sceneId": 1001,
                    "state": "Running",
                    "position": {"x": 12.3, "y": 0.0, "z": 45.6},
                    "controllable": True,
                },
                "availableSkills": [
                    {
                        "skillName": "observe_state",
                        "schemaVersion": "v1",
                        "requireRunning": True,
                    }
                ],
                "skillArgumentHints": [
                    {
                        "skillName": "observe_state",
                        "schemaVersion": "v1",
                        "argumentStatus": "ready",
                        "allowedArgs": [],
                        "missingArgs": [],
                    }
                ],
                "lastSkillResult": None,
            },
        }
    ],
}

_GATEWAY_EVENTS_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["traceId", "gatewayId", "contractVersion", "events"],
    "properties": {
        "traceId": {"type": "string", "minLength": 1},
        "gatewayId": {"type": "string", "minLength": 1},
        "contractVersion": {"type": "string", "const": "llm-gateway-http-v1"},
        "sentAtMs": {"type": "integer"},
        "events": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "eventId",
                    "eventType",
                    "sessionId",
                    "stateVersion",
                    "decisionLeaseId",
                    "occurredAtMs",
                    "payload",
                ],
                "properties": {
                    "eventId": {"type": "string", "minLength": 1},
                    "eventType": {
                        "type": "string",
                        "enum": ["observation_updated", "decision_rejected", "skill_finished"],
                    },
                    "sessionId": {"type": "string", "minLength": 1},
                    "stateVersion": {"type": "integer", "minimum": 0},
                    "decisionLeaseId": {"type": "string", "minLength": 1},
                    "occurredAtMs": {"type": "integer"},
                    "payload": {
                        "type": "object",
                        "required": ["eventType", "session"],
                        "properties": {
                            "eventType": {
                                "type": "string",
                                "enum": ["observation_updated", "decision_rejected", "skill_finished"],
                            },
                            "reason": {"type": ["string", "null"]},
                            "session": {
                                "type": "object",
                                "required": ["sessionId", "accountId", "roleId"],
                                "properties": {
                                    "sessionId": {"type": "string"},
                                    "accountId": {"type": "string"},
                                    "roleId": {"type": "string"},
                                    "sceneId": {"type": ["integer", "null"]},
                                    "state": {"type": ["string", "null"]},
                                    "position": {
                                        "type": ["object", "null"],
                                        "properties": {
                                            "x": {"type": "number"},
                                            "y": {"type": "number"},
                                            "z": {"type": "number"},
                                        },
                                    },
                                    "controllable": {"type": ["boolean", "null"]},
                                },
                            },
                            "availableSkills": {"type": "array", "items": {"type": "object"}},
                            "skillArgumentHints": {"type": "array", "items": {"type": "object"}},
                            "lastSkillResult": {"type": ["object", "null"]},
                        },
                    },
                },
            },
        },
    },
}

_GATEWAY_EVENTS_OPENAPI_EXTRA = {
    "parameters": [
        {
            "name": "X-AppId",
            "in": "header",
            "required": True,
            "description": "Gateway HMAC AppId，必须存在于服务端 LLM_GATEWAY_APP_SECRETS 配置中。",
            "schema": {"type": "string"},
            "example": "gateway-to-llm",
        },
        {
            "name": "X-TimestampMs",
            "in": "header",
            "required": True,
            "description": "生成签名时使用的当前 Unix 毫秒时间戳。",
            "schema": {"type": "string", "pattern": "^[0-9]+$"},
        },
        {
            "name": "X-RequestId",
            "in": "header",
            "required": True,
            "description": "本次请求的幂等请求标识；Gateway 重试时保持不变。",
            "schema": {"type": "string"},
            "example": "req-001",
        },
        {
            "name": "X-Signature",
            "in": "header",
            "required": True,
            "description": "按原始请求体计算的十六进制 HMAC-SHA256 签名。",
            "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    ],
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": _GATEWAY_EVENTS_REQUEST_SCHEMA,
                "example": _GATEWAY_EVENTS_EXAMPLE,
            }
        },
    },
}

_GATEWAY_PROTOCOL_RESPONSES = {
    400: {"description": "请求体、事件契约或幂等内容冲突。"},
    401: {"description": "HMAC 身份、时间戳或签名无效。"},
    500: {"description": "事件写入持久队列失败。"},
    503: {"description": "LLM Gateway v1 runtime 已禁用。"},
}


def _protocol_error(code: str, status_code: int) -> JSONResponse:
    messages = {
        "bad_request": "bad request",
        "signature_invalid": "request signature invalid",
        "timestamp_expired": "request timestamp expired",
        "internal_error": "internal error",
        "service_disabled": "service disabled",
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
    if not hmac.compare_digest(signature, expected):
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


def _parse_gateway_event_batch_envelope(raw_body: bytes) -> GatewayEventBatchEnvelope | None:
    try:
        payload = json.loads(raw_body)
        return GatewayEventBatchEnvelope.model_validate(payload)
    except Exception:
        return None


def _json_body_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _gateway_event_record(batch: GatewayEventBatchEnvelope, event: GatewayEvent) -> dict[str, Any]:
    return {
        "traceId": batch.trace_id,
        "gatewayId": batch.gateway_id,
        "contractVersion": batch.contract_version,
        "event": event.model_dump(mode="json", by_alias=True, exclude_none=False),
    }


def _gateway_event_ids_are_valid(event: GatewayEvent) -> bool:
    values = (
        event.event_id,
        event.session_id,
        event.decision_lease_id,
        event.payload.session.session_id,
        event.payload.session.account_id,
        event.payload.session.role_id,
    )
    return all(value is None or _valid_id(value) for value in values)


def _gateway_batch_response(
    trace_id: str,
    received_event_ids: list[str],
    duplicate_event_ids: list[str],
) -> dict[str, Any]:
    return {
        "accepted": True,
        "traceId": trace_id,
        "receivedEventIds": received_event_ids,
        "duplicateEventIds": duplicate_event_ids,
    }


class PlayerEvent(BaseModel):
    user_id: str = Field(..., description="玩家 ID")
    event_type: str = Field(..., description="事件类型: online / offline / behavior_checkpoint")
    timestamp: float = Field(..., description="事件时间戳")
    snapshot: dict | None = Field(default=None, description="玩家快照数据（可选）")
    # behavior_checkpoint 专用字段
    session_id: str | None = Field(default=None, description="会话 ID（behavior_checkpoint 必填）")
    behavior_event: dict | None = Field(default=None, description="行为事件详情")


@gateway_router.post(
    "/events",
    responses=_GATEWAY_PROTOCOL_RESPONSES,
    openapi_extra=_GATEWAY_EVENTS_OPENAPI_EXTRA,
)
async def handle_gateway_event(request: Request):
    """接收 LLM Gateway v1 runtime 事件。"""
    raw_body_or_error = await _validate_gateway_event_request(request)
    if isinstance(raw_body_or_error, JSONResponse):
        return raw_body_or_error
    raw_body = raw_body_or_error

    batch = _parse_gateway_event_batch_envelope(raw_body)
    if batch is None:
        return _protocol_error("bad_request", 400)
    if not bool(getattr(_settings(), "llm_gateway_v1_enabled", True)):
        return _protocol_error("service_disabled", 503)

    received_event_ids: list[str] = []
    duplicate_event_ids: list[str] = []
    for event in batch.events:
        if not _gateway_event_ids_are_valid(event):
            return _protocol_error("bad_request", 400)
        record = _gateway_event_record(batch, event)
        event_body_hash = sha256(_json_body_bytes(record)).hexdigest()
        try:
            status = await enqueue_gateway_event(
                event_id=event.event_id,
                body_sha256=event_body_hash,
                record=record,
            )
        except Exception as error:
            logger.error(
                "Gateway event enqueue failed, event_id=%s",
                event.event_id,
                extra=safe_exception_fields(
                    stage="event_queue",
                    category="enqueue_failed",
                    error=error,
                    event_id=event.event_id,
                    trace_id=batch.trace_id,
                ),
            )
            return _protocol_error("internal_error", 500)
        if status == "accepted":
            received_event_ids.append(event.event_id)
        elif status == "duplicate":
            duplicate_event_ids.append(event.event_id)
        else:
            return _protocol_error("bad_request", 400)

    return _gateway_batch_response(batch.trace_id, received_event_ids, duplicate_event_ids)


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


async def process_gateway_event_record(record: dict[str, Any]) -> None:
    """消费 Redis Stream 中的 Gateway 事件并提交一次决策。"""
    batch_fields = {
        "traceId": record.get("traceId"),
        "gatewayId": record.get("gatewayId"),
        "contractVersion": record.get("contractVersion"),
        "events": [record.get("event")],
    }
    batch = GatewayEventBatchEnvelope.model_validate(batch_fields)
    event = batch.events[0]
    logger.info(
        "[gateway_v1] event processing, trace_id=%s, event_id=%s, event_type=%s, session_id=%s",
        batch.trace_id,
        event.event_id,
        event.event_type,
        event.session_id,
    )

    current_settings = _settings()
    decision_url = getattr(current_settings, "llm_gateway_decision_url", None)
    decision_app_id = getattr(current_settings, "llm_gateway_decision_app_id", None)
    decision_app_secret = getattr(current_settings, "llm_gateway_decision_app_secret", None)
    if not decision_url or not decision_app_id or not decision_app_secret:
        raise RuntimeError("Gateway decision client is not configured")

    output = await run_gateway_v1_agent(
        tenant_id=_tenant_id_for_gateway_event(batch.gateway_id),
        trace_id=batch.trace_id,
        event_id=event.event_id,
        event_type=event.event_type,
        session_id=event.session_id,
        state_version=event.state_version,
        decision_lease_id=event.decision_lease_id,
        session=event.payload.session.model_dump(mode="json", by_alias=True, exclude_none=False),
        event_payload=event.payload.model_dump(mode="json", by_alias=True, exclude_none=False),
    )
    actions = output.get("recommended_actions") if isinstance(output, dict) else []
    recommended_action = _select_gateway_action(actions or [], event.payload)

    await send_llm_gateway_decision(
        decision_url=decision_url,
        app_id=decision_app_id,
        app_secret=decision_app_secret,
        timeout_seconds=float(getattr(current_settings, "llm_gateway_decision_timeout_seconds", 10.0)),
        trace_id=batch.trace_id,
        session_id=event.session_id,
        decision_id=f"decision-{event.event_id}",
        decision_lease_id=event.decision_lease_id,
        state_version=event.state_version,
        recommended_action=recommended_action,
        max_retries=int(getattr(current_settings, "llm_gateway_decision_max_retries", 0)),
    )


def _tenant_id_for_gateway_event(gateway_id: str) -> str:
    app_tenants = getattr(_settings(), "llm_gateway_app_tenants", {}) or {}
    if isinstance(app_tenants, dict):
        tenant_id = app_tenants.get(gateway_id)
        if isinstance(tenant_id, str) and tenant_id:
            return tenant_id
    return gateway_id


def _mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _skill_name(skill: dict[str, Any]) -> str | None:
    return _mapping_value(skill, "skillName", "SkillName")


def _schema_version(skill: dict[str, Any]) -> str | None:
    return _mapping_value(skill, "schemaVersion", "SchemaVersion")


def _flatten_argument_paths(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths.update(_flatten_argument_paths(child, path))
        return paths
    if isinstance(value, list):
        paths = set()
        for child in value:
            paths.update(_flatten_argument_paths(child, f"{prefix}[]"))
        return paths
    return {prefix} if prefix else set()


def _action_matches_hint(action: dict[str, Any], hint: dict[str, Any] | None) -> bool:
    if hint is None:
        return True
    missing_args = _mapping_value(hint, "missingArgs", "MissingArgs") or []
    if missing_args:
        return False
    arguments = action.get("arguments", {})
    if not isinstance(arguments, dict):
        return False
    allowed_args = _mapping_value(hint, "allowedArgs", "AllowedArgs") or []
    allowed_paths = {
        str(_mapping_value(item, "path", "Path"))
        for item in allowed_args
        if isinstance(item, dict) and _mapping_value(item, "path", "Path")
    }
    if not allowed_paths:
        return not arguments
    argument_paths = _flatten_argument_paths(arguments)
    return all(
        path in allowed_paths or any(path.startswith(f"{allowed}.") for allowed in allowed_paths)
        for path in argument_paths
    )


def _select_gateway_action(actions: list[dict[str, Any]], payload: GatewayEventPayload) -> dict[str, Any]:
    available = {
        (_skill_name(item), _schema_version(item)): item for item in payload.available_skills if _skill_name(item)
    }
    hints = {
        (_skill_name(item), _schema_version(item)): item for item in payload.skill_argument_hints if _skill_name(item)
    }
    for action in actions:
        if not isinstance(action, dict):
            continue
        skill_name = action.get("skillName") or action.get("SkillName")
        schema_version = action.get("schemaVersion") or action.get("SchemaVersion") or "v1"
        if (skill_name, schema_version) not in available:
            continue
        if not _action_matches_hint(action, hints.get((skill_name, schema_version))):
            continue
        normalized = dict(action)
        normalized["skillName"] = skill_name
        normalized["schemaVersion"] = schema_version
        return normalized

    return {
        "action": "wait",
        "arguments": {"waitMs": 1000},
        "reason": "Agent 没有生成当前 Gateway 允许且参数完整的 skill，等待下一次观察",
        "confidence": 0.0,
        "ttlMs": 1000,
    }


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
    event_payload: dict[str, Any],
) -> dict:
    """用 v1 Gateway event 触发现有 LangGraph Agent。"""
    user_id = (
        session.get("roleId")
        or session.get("RoleId")
        or session.get("accountId")
        or session.get("AccountId")
        or session.get("sessionId")
        or session.get("SessionId")
    )
    snapshot = {
        "user_id": user_id,
        "traceId": trace_id,
        "eventId": event_id,
        "eventType": event_type,
        "sessionId": session_id,
        "stateVersion": state_version,
        "decisionLeaseId": decision_lease_id,
        "session": session,
        "eventReason": event_payload.get("reason"),
        "availableSkills": event_payload.get("availableSkills", []),
        "skillArgumentHints": event_payload.get("skillArgumentHints", []),
        "lastSkillResult": event_payload.get("lastSkillResult"),
        "position": session.get("position") or session.get("Position"),
        "sceneId": session.get("sceneId") or session.get("SceneId"),
        "accountId": session.get("accountId") or session.get("AccountId"),
        "roleId": session.get("roleId") or session.get("RoleId"),
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
    event_data_json = json.dumps(event_data, ensure_ascii=False) if event_data is not None else None
    snapshot_json = json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None

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
                "event_data": event_data_json,
                "snapshot": snapshot_json,
            },
        )
    logger.debug(
        "[webhook] 行为事件已写入, user_id=%s, session_id=%s, type=%s",
        user_id,
        session_id,
        event_type,
    )
