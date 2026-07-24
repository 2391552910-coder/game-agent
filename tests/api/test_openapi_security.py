"""OpenAPI 安全声明测试。"""


def test_player_event_openapi_declares_api_key_security():
    from src.api.main import app

    schema = app.openapi()
    security_schemes = schema["components"]["securitySchemes"]
    assert security_schemes["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    operation = schema["paths"]["/webhooks/player-event"]["post"]
    assert {"APIKeyHeader": []} in operation["security"]


def test_gateway_events_openapi_does_not_require_tenant_api_key():
    from src.api.main import app

    operation = app.openapi()["paths"]["/api/gateway/events"]["post"]
    assert "security" not in operation


def test_gateway_events_openapi_documents_hmac_headers_and_event_body():
    from src.api.main import app

    operation = app.openapi()["paths"]["/api/gateway/events"]["post"]
    headers = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
    }
    assert set(headers) == {"X-AppId", "X-TimestampMs", "X-RequestId", "X-Signature"}
    assert all(parameter["required"] for parameter in headers.values())

    body = operation["requestBody"]
    assert body["required"] is True
    media_type = body["content"]["application/json"]
    assert media_type["schema"]["properties"]["contractVersion"]["const"] == "llm-gateway-http-v1"
    assert media_type["example"]["events"][0]["eventType"] == "observation_updated"


def test_gateway_events_openapi_documents_protocol_responses():
    from src.api.main import app

    responses = app.openapi()["paths"]["/api/gateway/events"]["post"]["responses"]
    assert {"200", "400", "401", "500"}.issubset(responses)
