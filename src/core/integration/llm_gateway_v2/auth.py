from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NoReturn, TypeAlias
from uuid import UUID

from pydantic import SecretStr

from src.config import settings

GatewayAuthErrorCode: TypeAlias = Literal[
    "auth_header_invalid",
    "auth_timestamp_invalid",
    "signature_invalid",
    "app_id_unknown",
    "gateway_not_authorized",
    "tenant_not_configured",
]
GatewayAuthHttpStatus: TypeAlias = Literal[400, 401]

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_LOWERCASE_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_PATTERN = re.compile(r"[0-9]{1,20}\Z")
_MAX_TIMESTAMP_MS = 99_999_999_999_999_999_999
_METHOD_TOKEN_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_REQUIRED_HEADERS = ("x-appid", "x-timestampms", "x-requestid", "x-signature")


@dataclass(frozen=True)
class GatewayAuthError(Exception):
    code: GatewayAuthErrorCode
    http_status: GatewayAuthHttpStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", ())

    def __str__(self) -> str:
        return ""


@dataclass(frozen=True)
class InboundGatewayIdentity:
    app_id: str
    gateway_id: str
    tenant_id: UUID


def _fail(code: GatewayAuthErrorCode, http_status: GatewayAuthHttpStatus) -> NoReturn:
    raise GatewayAuthError(code=code, http_status=http_status)


def _validate_request_components(method: object, path: object, raw_body: object) -> tuple[str, str, bytes]:
    if not isinstance(method, str) or _METHOD_TOKEN_PATTERN.fullmatch(method) is None:
        _fail("auth_header_invalid", 400)
    if not isinstance(path, str) or not path.startswith("/") or "\r" in path or "\n" in path:
        _fail("auth_header_invalid", 400)
    if type(raw_body) is not bytes:
        _fail("auth_header_invalid", 400)
    return method, path, raw_body


def _validate_identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        _fail("auth_header_invalid", 400)
    return value


def _normalize_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        _fail("auth_header_invalid", 400)

    normalized: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            _fail("auth_header_invalid", 400)
        name = raw_name.lower()
        if name in normalized:
            _fail("auth_header_invalid", 400)
        normalized[name] = raw_value

    if any(name not in normalized for name in _REQUIRED_HEADERS):
        _fail("auth_header_invalid", 400)
    return normalized


def _parse_timestamp(value: object) -> int:
    if not isinstance(value, str) or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        _fail("auth_timestamp_invalid", 400)
    try:
        return int(value)
    except ValueError:
        _fail("auth_timestamp_invalid", 400)


def _validate_non_negative_timestamp(value: object) -> int:
    if type(value) is not int or value < 0 or value > _MAX_TIMESTAMP_MS:
        _fail("auth_timestamp_invalid", 400)
    return value


def _secret_text(value: object) -> str | None:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if not isinstance(value, str) or not value:
        return None
    return value


def _build_signature(
    *,
    method: str,
    path: str,
    raw_body: bytes,
    app_secret: str,
    request_id: str,
    timestamp_text: str,
) -> str:
    body_hash = hashlib.sha256(raw_body).hexdigest()
    signing_text = "\n".join([method.upper(), path, timestamp_text, request_id, body_hash])
    return hmac.new(app_secret.encode(), signing_text.encode(), hashlib.sha256).hexdigest()


def verify_inbound_hmac(
    method: str,
    path: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    now_ms: int,
) -> str:
    method, path, raw_body = _validate_request_components(method, path, raw_body)
    now_ms = _validate_non_negative_timestamp(now_ms)
    normalized = _normalize_headers(headers)

    app_id = _validate_identifier(normalized["x-appid"])
    request_id = _validate_identifier(normalized["x-requestid"])
    signature = normalized["x-signature"]
    if _LOWERCASE_SHA256_PATTERN.fullmatch(signature) is None:
        _fail("auth_header_invalid", 400)

    timestamp_text = normalized["x-timestampms"]
    timestamp_ms = _parse_timestamp(timestamp_text)
    tolerance_ms = getattr(settings, "llm_gateway_timestamp_tolerance_ms", None)
    if type(tolerance_ms) is not int or tolerance_ms < 0:
        _fail("auth_timestamp_invalid", 400)
    if abs(now_ms - timestamp_ms) > tolerance_ms:
        _fail("auth_timestamp_invalid", 401)

    app_gateways = getattr(settings, "llm_gateway_app_gateways", None)
    if not isinstance(app_gateways, Mapping) or app_id not in app_gateways:
        _fail("app_id_unknown", 401)
    app_secrets = getattr(settings, "llm_gateway_app_secrets", None)
    if not isinstance(app_secrets, Mapping):
        _fail("app_id_unknown", 401)
    app_secret = _secret_text(app_secrets.get(app_id))
    if app_secret is None:
        _fail("app_id_unknown", 401)

    expected = _build_signature(
        method=method,
        path=path,
        raw_body=raw_body,
        app_secret=app_secret,
        request_id=request_id,
        timestamp_text=timestamp_text,
    )
    if not hmac.compare_digest(signature, expected):
        _fail("signature_invalid", 401)
    return app_id


def resolve_inbound_identity(app_id: str, gateway_id: str) -> InboundGatewayIdentity:
    app_gateways = getattr(settings, "llm_gateway_app_gateways", None)
    if not isinstance(app_gateways, Mapping) or app_id not in app_gateways:
        _fail("app_id_unknown", 401)

    allowed_gateway_ids = app_gateways[app_id]
    if (
        not isinstance(gateway_id, str)
        or isinstance(allowed_gateway_ids, (str, bytes))
        or not isinstance(allowed_gateway_ids, (list, tuple, set, frozenset))
        or gateway_id not in allowed_gateway_ids
    ):
        _fail("gateway_not_authorized", 401)

    app_tenants = getattr(settings, "llm_gateway_app_tenants", None)
    if not isinstance(app_tenants, Mapping):
        _fail("tenant_not_configured", 400)
    tenant_value = app_tenants.get(gateway_id)
    try:
        tenant_id = tenant_value if isinstance(tenant_value, UUID) else UUID(tenant_value)
    except (AttributeError, TypeError, ValueError):
        _fail("tenant_not_configured", 400)
    return InboundGatewayIdentity(app_id=app_id, gateway_id=gateway_id, tenant_id=tenant_id)


def build_outbound_hmac_headers(
    method: str,
    path: str,
    raw_body: bytes,
    app_id: str,
    app_secret: SecretStr,
    request_id: str,
    timestamp_ms: int,
) -> dict[str, str]:
    method, path, raw_body = _validate_request_components(method, path, raw_body)
    app_id = _validate_identifier(app_id)
    request_id = _validate_identifier(request_id)
    timestamp_ms = _validate_non_negative_timestamp(timestamp_ms)
    if not isinstance(app_secret, SecretStr):
        _fail("auth_header_invalid", 400)
    secret_text = _secret_text(app_secret)
    if secret_text is None:
        _fail("auth_header_invalid", 400)

    timestamp_text = str(timestamp_ms)
    signature = _build_signature(
        method=method,
        path=path,
        raw_body=raw_body,
        app_secret=secret_text,
        request_id=request_id,
        timestamp_text=timestamp_text,
    )
    return {
        "Content-Type": "application/json",
        "X-AppId": app_id,
        "X-TimestampMs": timestamp_text,
        "X-RequestId": request_id,
        "X-Signature": signature,
    }
