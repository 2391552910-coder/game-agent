from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.core.integration.llm_gateway_v2.decision_client import validate_decision_response
from src.core.integration.llm_gateway_v2.monitor_audit import MonitorAuditRepository, MonitorRecord


def test_monitor_record_requires_safe_structured_error_fields() -> None:
    record = MonitorRecord(
        record_type="error",
        direction="system",
        status="failed",
        gateway_id="gateway-1",
        error_stage="http",
        error_category="timeout",
    )

    assert record.error_detail is None
    assert record.record_type == "error"
    assert record.direction == "system"


def test_monitor_query_does_not_duplicate_lifecycle_records_and_keeps_legacy_decision_response() -> None:
    from src.core.integration.llm_gateway_v2 import monitor_repository

    sql = monitor_repository._BASE_RECORDS_SQL
    assert "a.record_type = 'chat'" in sql
    assert "a.direction = 'outbound'" in sql
    assert "COALESCE(" in sql
    assert "d.response_body_json" in sql
    assert "WHEN d.response_http_status IS NULL" in sql
    assert "托管 Agent 对话" in sql


def test_monitor_query_casts_nullable_filter_parameters_for_asyncpg() -> None:
    from src.core.integration.llm_gateway_v2 import monitor_repository

    sql = monitor_repository._BASE_RECORDS_SQL

    assert "CAST(:kind AS TEXT)" in sql
    assert "CAST(:cursor_at AS TIMESTAMPTZ)" in sql


def test_monitor_query_template_formats_without_interpreting_json_path_braces() -> None:
    from src.core.integration.llm_gateway_v2 import monitor_repository

    rendered = monitor_repository._BASE_RECORDS_SQL.format(cursor_comparison="TRUE", order="DESC")

    assert "#>> '{payload,text}'" in rendered


@pytest.mark.asyncio
async def test_monitor_repository_appends_structured_record() -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    class _Result:
        def scalar_one(self) -> int:
            return 42

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def begin(self) -> _Session:
            return self

        async def execute(self, statement: object, params: dict[str, object]) -> _Result:
            calls.append((statement, params))
            return _Result()

    class _Factory:
        def __call__(self) -> _Session:
            return _Session()

    repository = MonitorAuditRepository(_Factory())
    row_id = await repository.append(
        MonitorRecord(
            tenant_id=uuid4(),
            gateway_id="gateway-1",
            session_id="session-1",
            event_id="event-1",
            record_type="event",
            direction="inbound",
            status="accepted",
            request_body_json={"eventType": "observation_updated"},
        )
    )

    assert row_id == 42
    assert calls
    params = calls[0][1]
    assert params["record_type"] == "event"
    assert params["request_body_json"] == {"eventType": "observation_updated"}


def test_decision_response_keeps_complete_response_json_for_audit() -> None:
    result = validate_decision_response(
        "call_skill",
        200,
        {
            "accepted": True,
            "status": "accepted",
            "traceId": "trace-1",
            "sessionId": "session-1",
            "decisionId": "decision-1",
            "decisionLeaseId": "lease-1",
            "controlGeneration": 1,
            "stateVersion": 2,
            "skillCallId": "call-1",
            "nextDecisionLeaseId": None,
            "reason": "ok",
        },
        request_identity={
            "traceId": "trace-1",
            "sessionId": "session-1",
            "decisionId": "decision-1",
            "decisionLeaseId": "lease-1",
            "controlGeneration": 1,
            "stateVersion": 2,
        },
    )

    assert result.response_body_json == {
        "accepted": True,
        "status": "accepted",
        "traceId": "trace-1",
        "sessionId": "session-1",
        "decisionId": "decision-1",
        "decisionLeaseId": "lease-1",
        "controlGeneration": 1,
        "stateVersion": 2,
        "skillCallId": "call-1",
        "nextDecisionLeaseId": None,
        "reason": "ok",
    }


@pytest.mark.asyncio
async def test_hosted_chat_audit_recorder_captures_inbound_generated_and_result() -> None:
    from src.core.integration.llm_gateway_v2.hosted_chat import (
        HostedChatSendReceipt,
        HostedChatService,
    )

    records: list[dict[str, object]] = []

    async def record(item: dict[str, object]) -> None:
        records.append(item)

    class _Conversation:
        async def generate(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                speaker_role_id=1,
                target_role_id=2,
                pair_key="1:2:event-1",
                content="generated content",
            )

    class _Identity:
        async def resolve_role_id(self, gateway_id: str, session_id: str) -> str:
            return "1"

    class _Sender:
        async def send(self, request: object, *, request_id: str | None = None) -> HostedChatSendReceipt:
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    service = HostedChatService(
        conversation_client=_Conversation(),
        identity_resolver=_Identity(),
        sender=_Sender(),
        audit_recorder=record,
    )
    event = SimpleNamespace(
        event_id="event-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="incoming content",
            sender=SimpleNamespace(avatar_id="2", role_id="2"),
            chat_type="private",
        ),
    )
    await service.handle_chat_received("gateway-1", event)
    await service.handle_send_result(
        "gateway-1",
        SimpleNamespace(
            event_id="event-2",
            payload=SimpleNamespace(
                chat_message_id="message-1",
                session_id="session-1",
                target=SimpleNamespace(avatar_id="2", role_id="2"),
                chat_type="private",
                status="sent",
                reason=None,
            ),
        ),
    )

    assert [item["status"] for item in records] == ["received", "generated", "sent"]
    assert records[0]["content"] == "incoming content"
    assert records[1]["content"] == "generated content"
    assert records[1]["request_body_json"]["content"] == "generated content"
