from __future__ import annotations

import inspect
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from src.core.agents.orchestrator import _timed_node
from src.core.integration import gateway_event_queue
from src.core.integration.llm_gateway_v2.errors import (
    GatewayV2OperationalError,
    safe_exception_fields,
)
from src.core.integration.robotgateway_callback import (
    RobotGatewayCallbackError,
    send_robotgateway_analysis_callback,
)
from src.logging_config import SENSITIVE_LOGGER_LEVELS, configure_logging

SECRET_TEXT = (
    "password=db-secret token=provider-secret prompt=private-prompt "
    "snapshot={accountId:7} sql_params={tenant_id:secret}"
)


def test_logging_is_configured_before_framework_and_infrastructure_imports() -> None:
    import src.api.main as main

    source = inspect.getsource(main)
    configured_at = source.index("configure_logging()")
    assert configured_at < source.index("from fastapi import")
    assert configured_at < source.index("from src.core.infrastructure.db import")


def test_sensitive_sdk_loggers_are_restricted_and_sql_echo_is_disabled() -> None:
    configure_logging()

    for name, expected_level in SENSITIVE_LOGGER_LEVELS.items():
        assert logging.getLogger(name).level >= expected_level

    from src.core.infrastructure.db import engine

    assert engine.echo is False


def test_safe_exception_fields_include_only_stable_diagnostics() -> None:
    error = RuntimeError(SECRET_TEXT)

    fields = safe_exception_fields(
        stage="agent",
        category="execution_failed",
        error=error,
        trace_id="trace-1",
        event_id="event-1",
        decision_id="decision-1",
        skill_call_id="call-1",
        elapsed_ms=12.5,
    )

    assert fields == {
        "stage": "agent",
        "error_category": "execution_failed",
        "exception_type": "RuntimeError",
        "trace_id": "trace-1",
        "event_id": "event-1",
        "decision_id": "decision-1",
        "skill_call_id": "call-1",
        "elapsed_ms": 12.5,
    }
    assert SECRET_TEXT not in repr(fields)


async def test_agent_failure_log_does_not_include_exception_message(caplog: pytest.LogCaptureFixture) -> None:
    async def failing_node(state):
        del state
        raise RuntimeError(SECRET_TEXT)

    wrapped = _timed_node("safe_agent_node", failing_node)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="password=db-secret"):
        await wrapped({})

    assert SECRET_TEXT not in caplog.text
    record = next(record for record in caplog.records if record.levelno == logging.ERROR)
    assert record.stage == "agent"
    assert record.error_category == "node_failed"
    assert record.exception_type == "RuntimeError"


async def test_event_processing_failure_log_does_not_include_payload_or_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = AsyncMock()

    async def processor(record):
        assert record["prompt"] == SECRET_TEXT
        raise RuntimeError(SECRET_TEXT)

    with caplog.at_level(logging.ERROR):
        await gateway_event_queue._process_messages(
            redis,
            [("stream-1", {"payload": json.dumps({"prompt": SECRET_TEXT})})],
            processor,
        )

    assert SECRET_TEXT not in caplog.text
    record = next(record for record in caplog.records if record.levelno == logging.ERROR)
    assert record.stage == "event"
    assert record.error_category == "processing_failed"
    assert record.exception_type == "RuntimeError"


async def test_startup_database_failure_is_rethrown_as_safe_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.api.main as main

    monkeypatch.setattr(main, "init_db", AsyncMock(side_effect=RuntimeError(SECRET_TEXT)))
    with caplog.at_level(logging.ERROR), pytest.raises(GatewayV2OperationalError) as raised:
        async with main.lifespan(main.app):
            pass

    assert str(raised.value) == "gateway v2 operation failed"
    assert raised.value.stage == "startup"
    assert SECRET_TEXT not in caplog.text
    record = next(record for record in caplog.records if record.levelno == logging.ERROR)
    assert record.error_category == "dependency_unavailable"
    assert record.exception_type == "RuntimeError"


async def test_callback_failure_does_not_expose_http_exception_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(SECRET_TEXT, request=request)

    with pytest.raises(RobotGatewayCallbackError) as raised:
        await send_robotgateway_analysis_callback(
            callback_url="http://gateway.invalid/callback",
            api_key="secret-key",
            timeout_seconds=1,
            tenant_id="tenant-1",
            user_id="user-1",
            snapshot={"private": SECRET_TEXT},
            output={},
            transport=httpx.MockTransport(handler),
        )

    assert str(raised.value) == "RobotGateway callback failed"
    assert raised.value.category == "request_failed"
    assert SECRET_TEXT not in str(raised.value)
