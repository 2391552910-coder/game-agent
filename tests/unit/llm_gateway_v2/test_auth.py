import base64
import hashlib
import hmac
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from src.core.integration.llm_gateway_v2 import auth
from src.core.integration.llm_gateway_v2.auth import (
    GatewayAuthError,
    InboundGatewayIdentity,
    build_outbound_hmac_headers,
    resolve_inbound_identity,
    verify_inbound_hmac,
)
from src.core.integration.robotgateway_callback import build_llm_gateway_hmac_headers

INBOUND_APP_ID = "gateway-events-v2"
_HMAC_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "llm_gateway_v2" / "hmac_vectors.json"
_HMAC_FIXTURE = json.loads(_HMAC_FIXTURE_PATH.read_text(encoding="utf-8"))
_EVENTS_VECTOR = _HMAC_FIXTURE["vectors"]["events"]
_DECISION_VECTOR = _HMAC_FIXTURE["vectors"]["decision"]

INBOUND_SECRET = _EVENTS_VECTOR["appSecret"]
OUTBOUND_APP_ID = "myagent2-decisions-v2"
OUTBOUND_SECRET = _DECISION_VECTOR["appSecret"]
V1_ONLY_APP_ID = "gateway-events-v1"
GATEWAY_ID = "gateway-v2"
TENANT_ID = UUID("7d9fc80e-aeb6-4b4a-a398-faaee84d69ab")
EVENTS_PATH = _EVENTS_VECTOR["path"]
DECISION_PATH = _DECISION_VECTOR["path"]
EVENTS_TIMESTAMP_MS = _EVENTS_VECTOR["timestampMs"]
EVENTS_REQUEST_ID = _EVENTS_VECTOR["requestId"]
EVENTS_BODY = base64.b64decode(_EVENTS_VECTOR["rawBodyBase64"], validate=True)
EVENTS_BODY_SHA256 = _EVENTS_VECTOR["bodySha256"]
EVENTS_SIGNATURE = _EVENTS_VECTOR["expectedSignature"]
DECISION_TIMESTAMP_MS = _DECISION_VECTOR["timestampMs"]
DECISION_REQUEST_ID = _DECISION_VECTOR["requestId"]
DECISION_BODY = base64.b64decode(_DECISION_VECTOR["rawBodyBase64"], validate=True)
DECISION_BODY_SHA256 = _DECISION_VECTOR["bodySha256"]
DECISION_SIGNATURE = _DECISION_VECTOR["expectedSignature"]


@pytest.fixture(autouse=True)
def gateway_settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    configured = SimpleNamespace(
        llm_gateway_app_secrets={
            INBOUND_APP_ID: INBOUND_SECRET,
            V1_ONLY_APP_ID: "v1-only-secret",
            OUTBOUND_APP_ID: OUTBOUND_SECRET,
        },
        llm_gateway_app_gateways={INBOUND_APP_ID: [GATEWAY_ID, "gateway-without-tenant", "gateway-bad-tenant"]},
        llm_gateway_app_tenants={
            GATEWAY_ID: str(TENANT_ID),
            "gateway-bad-tenant": "not-a-uuid",
        },
        llm_gateway_timestamp_tolerance_ms=300_000,
        llm_gateway_decision_app_id=OUTBOUND_APP_ID,
    )
    monkeypatch.setattr(auth, "settings", configured)
    return configured


def _signature(*, method: str, path: str, body: bytes, timestamp_ms: int, request_id: str, secret: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join([method.upper(), path, str(timestamp_ms), request_id, body_hash])
    return hmac.new(secret.encode(), signing_text.encode(), hashlib.sha256).hexdigest()


def _headers(
    *,
    app_id: str = INBOUND_APP_ID,
    secret: str = INBOUND_SECRET,
    method: str = "POST",
    path: str = EVENTS_PATH,
    body: bytes = EVENTS_BODY,
    timestamp_ms: int = EVENTS_TIMESTAMP_MS,
    request_id: str = EVENTS_REQUEST_ID,
) -> dict[str, str]:
    return {
        "X-AppId": app_id,
        "X-TimestampMs": str(timestamp_ms),
        "X-RequestId": request_id,
        "X-Signature": _signature(
            method=method,
            path=path,
            body=body,
            timestamp_ms=timestamp_ms,
            request_id=request_id,
            secret=secret,
        ),
    }


def _assert_auth_error(error: GatewayAuthError, code: str, http_status: int) -> None:
    assert error.code == code
    assert error.http_status == http_status
    assert str(error) == ""
    assert error.args == ()
    assert set(vars(error)) <= {"code", "http_status"}


def test_events_fixed_vector_is_gateway_export_and_verifies() -> None:
    assert _HMAC_FIXTURE["fixtureOnly"] is True
    assert _HMAC_FIXTURE["source"]["commit"] == "fee33bf012b590807df502e658de88f20f3c6dd0"
    assert hashlib.sha256(EVENTS_BODY).hexdigest() == EVENTS_BODY_SHA256
    headers = {
        "X-AppId": INBOUND_APP_ID,
        "X-TimestampMs": str(EVENTS_TIMESTAMP_MS),
        "X-RequestId": EVENTS_REQUEST_ID,
        "X-Signature": EVENTS_SIGNATURE,
    }

    app_id = verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    assert app_id == INBOUND_APP_ID


def test_inbound_headers_are_case_insensitive() -> None:
    headers = {
        "x-appid": INBOUND_APP_ID,
        "X-TIMESTAMPMS": str(EVENTS_TIMESTAMP_MS),
        "x-ReQuEsTiD": EVENTS_REQUEST_ID,
        "X-sIgNaTuRe": EVENTS_SIGNATURE,
    }

    assert verify_inbound_hmac("post", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS) == INBOUND_APP_ID


@pytest.mark.parametrize("missing_header", ["X-AppId", "X-TimestampMs", "X-RequestId", "X-Signature"])
def test_missing_required_header_is_rejected(missing_header: str) -> None:
    headers = _headers()
    del headers[missing_header]

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_header_invalid", 400)


def test_duplicate_case_insensitive_header_is_rejected() -> None:
    headers = _headers()
    headers["x-appid"] = INBOUND_APP_ID

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_header_invalid", 400)


@pytest.mark.parametrize(
    "field,value",
    [
        ("X-AppId", ""),
        ("X-AppId", "contains space"),
        ("X-AppId", "a" * 129),
        ("X-AppId", "网关"),
        ("X-RequestId", ""),
        ("X-RequestId", "contains/slash"),
        ("X-RequestId", "r" * 129),
        ("X-RequestId", "请求"),
        ("X-Signature", "A" * 64),
        ("X-Signature", "0" * 63),
        ("X-Signature", "g" * 64),
    ],
)
def test_malformed_identity_or_signature_header_is_rejected(field: str, value: str) -> None:
    headers = _headers()
    headers[field] = value

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_header_invalid", 400)


@pytest.mark.parametrize("timestamp", ["", "-1", "+1", " 1", "1 ", "1.0", "1e3", "not-a-time"])
def test_malformed_timestamp_header_has_stable_error(timestamp: str) -> None:
    headers = _headers()
    headers["X-TimestampMs"] = timestamp

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_timestamp_invalid", 400)


@pytest.mark.parametrize("timestamp", ["9" * 21, "9" * 5_000], ids=["21-digits", "5000-digits"])
def test_oversized_decimal_timestamp_is_safely_rejected(timestamp: str) -> None:
    headers = _headers()
    headers["X-TimestampMs"] = timestamp

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_timestamp_invalid", 400)


def test_twenty_digit_timestamp_parses_then_fails_time_window() -> None:
    headers = _headers()
    headers["X-TimestampMs"] = "9" * 20

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_timestamp_invalid", 401)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("", EVENTS_PATH, EVENTS_BODY),
        ("POST\nGET", EVENTS_PATH, EVENTS_BODY),
        ("PÖST", EVENTS_PATH, EVENTS_BODY),
        ("POST", "api/gateway/v2/events", EVENTS_BODY),
        ("POST", "", EVENTS_BODY),
        ("POST", EVENTS_PATH, bytearray(EVENTS_BODY)),
    ],
)
def test_invalid_request_components_are_rejected(method: str, path: str, body: object) -> None:
    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac(method, path, body, _headers(), EVENTS_TIMESTAMP_MS)  # type: ignore[arg-type]

    _assert_auth_error(caught.value, "auth_header_invalid", 400)


@pytest.mark.parametrize("now_ms", [-1, True, 1.5, "1719999999000"])
def test_invalid_current_time_is_rejected(now_ms: object) -> None:
    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, _headers(), now_ms)  # type: ignore[arg-type]

    _assert_auth_error(caught.value, "auth_timestamp_invalid", 400)


@pytest.mark.parametrize("offset_ms", [-300_000, 300_000])
def test_timestamp_window_boundaries_are_inclusive(offset_ms: int) -> None:
    timestamp_ms = EVENTS_TIMESTAMP_MS + offset_ms
    headers = _headers(timestamp_ms=timestamp_ms)

    assert verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS) == INBOUND_APP_ID


@pytest.mark.parametrize("offset_ms", [-300_001, 300_001])
def test_timestamp_outside_window_is_rejected(offset_ms: int) -> None:
    timestamp_ms = EVENTS_TIMESTAMP_MS + offset_ms
    headers = _headers(timestamp_ms=timestamp_ms)

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "auth_timestamp_invalid", 401)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", EVENTS_PATH, EVENTS_BODY),
        ("POST", "/api/gateway/v2/other", EVENTS_BODY),
        ("POST", EVENTS_PATH, EVENTS_BODY + b" "),
    ],
)
def test_signed_request_tampering_is_rejected(method: str, path: str, body: bytes) -> None:
    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac(method, path, body, _headers(), EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "signature_invalid", 401)


def test_well_formed_but_wrong_signature_is_rejected() -> None:
    headers = _headers()
    headers["X-Signature"] = "0" * 64

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "signature_invalid", 401)


@pytest.mark.parametrize(
    "app_id",
    ["unknown-v2-app", V1_ONLY_APP_ID, OUTBOUND_APP_ID],
    ids=["unknown", "v1-only", "outbound"],
)
def test_non_v2_app_id_is_rejected_before_signature_verification(app_id: str) -> None:
    headers = _headers(app_id=app_id, secret="a-secret-that-must-not-matter")

    with pytest.raises(GatewayAuthError) as caught:
        verify_inbound_hmac("POST", EVENTS_PATH, EVENTS_BODY, headers, EVENTS_TIMESTAMP_MS)

    _assert_auth_error(caught.value, "app_id_unknown", 401)


def test_resolve_inbound_identity_returns_bound_uuid() -> None:
    identity = resolve_inbound_identity(INBOUND_APP_ID, GATEWAY_ID)

    assert identity == InboundGatewayIdentity(INBOUND_APP_ID, GATEWAY_ID, TENANT_ID)
    assert isinstance(identity.tenant_id, UUID)


def test_inbound_identity_is_frozen() -> None:
    identity = resolve_inbound_identity(INBOUND_APP_ID, GATEWAY_ID)

    with pytest.raises(FrozenInstanceError):
        identity.gateway_id = "other"  # type: ignore[misc]


def test_resolve_rejects_unknown_or_non_v2_app() -> None:
    for app_id in ("unknown-v2-app", V1_ONLY_APP_ID, OUTBOUND_APP_ID):
        with pytest.raises(GatewayAuthError) as caught:
            resolve_inbound_identity(app_id, GATEWAY_ID)
        _assert_auth_error(caught.value, "app_id_unknown", 401)


def test_resolve_rejects_gateway_outside_app_allowlist() -> None:
    with pytest.raises(GatewayAuthError) as caught:
        resolve_inbound_identity(INBOUND_APP_ID, "other-gateway")

    _assert_auth_error(caught.value, "gateway_not_authorized", 401)


@pytest.mark.parametrize("gateway_id", ["gateway-without-tenant", "gateway-bad-tenant"])
def test_resolve_rejects_missing_or_non_uuid_tenant(gateway_id: str) -> None:
    with pytest.raises(GatewayAuthError) as caught:
        resolve_inbound_identity(INBOUND_APP_ID, gateway_id)

    _assert_auth_error(caught.value, "tenant_not_configured", 400)


def test_gateway_id_is_never_used_as_tenant_id() -> None:
    auth.settings.llm_gateway_app_gateways = {INBOUND_APP_ID: [str(TENANT_ID)]}
    auth.settings.llm_gateway_app_tenants = {}

    with pytest.raises(GatewayAuthError) as caught:
        resolve_inbound_identity(INBOUND_APP_ID, str(TENANT_ID))

    _assert_auth_error(caught.value, "tenant_not_configured", 400)


def test_decision_fixed_vector_builds_exact_headers() -> None:
    assert hashlib.sha256(DECISION_BODY).hexdigest() == DECISION_BODY_SHA256

    headers = build_outbound_hmac_headers(
        "POST",
        DECISION_PATH,
        DECISION_BODY,
        OUTBOUND_APP_ID,
        SecretStr(OUTBOUND_SECRET),
        DECISION_REQUEST_ID,
        DECISION_TIMESTAMP_MS,
    )

    assert headers == {
        "Content-Type": "application/json",
        "X-AppId": OUTBOUND_APP_ID,
        "X-TimestampMs": str(DECISION_TIMESTAMP_MS),
        "X-RequestId": DECISION_REQUEST_ID,
        "X-Signature": DECISION_SIGNATURE,
    }
    assert OUTBOUND_SECRET not in repr(headers)


def test_v2_builder_matches_existing_v1_hmac_algorithm() -> None:
    v2_headers = build_outbound_hmac_headers(
        "post",
        DECISION_PATH,
        DECISION_BODY,
        OUTBOUND_APP_ID,
        SecretStr(OUTBOUND_SECRET),
        DECISION_REQUEST_ID,
        DECISION_TIMESTAMP_MS,
    )
    v1_headers = build_llm_gateway_hmac_headers(
        method="post",
        path=DECISION_PATH,
        body=DECISION_BODY,
        app_id=OUTBOUND_APP_ID,
        app_secret=OUTBOUND_SECRET,
        request_id=DECISION_REQUEST_ID,
        timestamp_ms=str(DECISION_TIMESTAMP_MS),
    )

    assert v2_headers == v1_headers


def test_outbound_builder_accepts_maximum_twenty_digit_timestamp() -> None:
    timestamp_ms = 99_999_999_999_999_999_999

    headers = build_outbound_hmac_headers(
        "POST",
        DECISION_PATH,
        DECISION_BODY,
        OUTBOUND_APP_ID,
        SecretStr(OUTBOUND_SECRET),
        DECISION_REQUEST_ID,
        timestamp_ms,
    )

    assert headers["X-TimestampMs"] == "99999999999999999999"
    assert len(headers["X-Signature"]) == 64


@pytest.mark.parametrize(
    "timestamp_ms",
    [100_000_000_000_000_000_000, 10**5_000],
    ids=["21-digits", "5001-digits"],
)
def test_outbound_builder_safely_rejects_oversized_integer_timestamp(timestamp_ms: int) -> None:
    with pytest.raises(GatewayAuthError) as caught:
        build_outbound_hmac_headers(
            "POST",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            timestamp_ms,
        )

    _assert_auth_error(caught.value, "auth_timestamp_invalid", 400)


@pytest.mark.parametrize(
    ("method", "path", "body", "app_id", "secret", "request_id", "timestamp_ms", "code"),
    [
        (
            "",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            1,
            "auth_header_invalid",
        ),
        (
            "POST\nGET",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            1,
            "auth_header_invalid",
        ),
        (
            "POST",
            "decision",
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            1,
            "auth_header_invalid",
        ),
        (
            "POST",
            DECISION_PATH,
            bytearray(DECISION_BODY),
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            1,
            "auth_header_invalid",
        ),
        (
            "POST",
            DECISION_PATH,
            DECISION_BODY,
            "bad app",
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            1,
            "auth_header_invalid",
        ),
        (
            "POST",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(""),
            DECISION_REQUEST_ID,
            1,
            "auth_header_invalid",
        ),
        (
            "POST",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            "bad/request",
            1,
            "auth_header_invalid",
        ),
        (
            "POST",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            -1,
            "auth_timestamp_invalid",
        ),
        (
            "POST",
            DECISION_PATH,
            DECISION_BODY,
            OUTBOUND_APP_ID,
            SecretStr(OUTBOUND_SECRET),
            DECISION_REQUEST_ID,
            True,
            "auth_timestamp_invalid",
        ),
    ],
)
def test_outbound_builder_rejects_invalid_inputs_without_secret_leakage(
    method: str,
    path: str,
    body: object,
    app_id: str,
    secret: SecretStr,
    request_id: str,
    timestamp_ms: object,
    code: str,
) -> None:
    with pytest.raises(GatewayAuthError) as caught:
        build_outbound_hmac_headers(
            method,
            path,
            body,  # type: ignore[arg-type]
            app_id,
            secret,
            request_id,
            timestamp_ms,  # type: ignore[arg-type]
        )

    _assert_auth_error(caught.value, code, 400)
    assert OUTBOUND_SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    ("code", "http_status"),
    [
        ("auth_header_invalid", 400),
        ("auth_timestamp_invalid", 400),
        ("auth_timestamp_invalid", 401),
        ("signature_invalid", 401),
        ("app_id_unknown", 401),
        ("gateway_not_authorized", 401),
        ("tenant_not_configured", 400),
    ],
)
def test_auth_error_is_frozen_and_contains_no_context(code: str, http_status: int) -> None:
    error = GatewayAuthError(code=code, http_status=http_status)  # type: ignore[arg-type]

    _assert_auth_error(error, code, http_status)
    with pytest.raises(FrozenInstanceError):
        error.code = "signature_invalid"  # type: ignore[misc]
