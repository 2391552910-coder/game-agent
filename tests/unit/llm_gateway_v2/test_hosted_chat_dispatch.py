from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.core.integration.llm_gateway_v2.event_service import GatewayV2EventDispatcher
from src.core.integration.llm_gateway_v2.event_worker import ClaimedGatewayEvent
from src.core.integration.llm_gateway_v2.hosted_chat import HostedChatRetryableError


def _conversation() -> dict[str, object]:
    return {
        "conversationId": "conv-100-200-1",
        "pairKey": "100:200",
        "speakerRoleId": 100,
        "targetRoleId": 200,
        "brainUsername": "conv-100",
        "historyRounds": [],
        "completedRounds": 0,
        "maxRounds": 6,
        "expiresAtMs": 1_000_000,
    }


class ChatStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_chat_received(self, gateway_id: str, event: object) -> None:
        self.calls.append("received")

    async def handle_nearby_friend_request(self, gateway_id: str, event: object) -> None:
        self.calls.append("nearby")

    async def handle_send_result(self, gateway_id: str, event: object) -> None:
        self.calls.append("result")


def _claimed(event_type: str, event: object) -> ClaimedGatewayEvent:
    return ClaimedGatewayEvent(
        row_id=uuid4(),
        tenant_id=uuid4(),
        cycle_id=uuid4(),
        gateway_id="gateway-1",
        session_id="session-1",
        event_id=f"event-{event_type}",
        event_type=event_type,
        control_generation=1,
        event_sequence=2,
        event=event,
        content_hash="a" * 64,
        trace_id="trace-1",
        claim_token=uuid4(),
        claimed_fence_version=0,
        attempt_count=1,
        locked_by="worker",
        lock_until=datetime.now(UTC) + timedelta(seconds=30),
    )


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_call"),
    [
        (
            "chat_received",
            {
                "sessionId": "session-1",
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "supported": False,
                "serverTimeMs": 1,
                "conversation": _conversation(),
            },
            "received",
        ),
        (
            "nearby_friend_chat_requested",
            {
                "sessionId": "session-1",
                "target": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "distance": 3.0,
                "friendChatCount": 0,
                "conversation": _conversation(),
            },
            "nearby",
        ),
        (
            "chat_send_result",
            {
                "sessionId": "session-1",
                "chatMessageId": "message-1",
                "target": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "status": "sent",
                "completedAtMs": 1,
            },
            "result",
        ),
    ],
)
@pytest.mark.asyncio
async def test_chat_event_dispatches_to_chat_service_without_decision_planner(
    event_type: str,
    payload: dict,
    expected_call: str,
) -> None:
    chat = ChatStub()
    planner_called = False

    async def planner(event: object, context: object):
        nonlocal planner_called
        planner_called = True
        raise AssertionError("chat event must not enter decision planner")

    dispatcher = GatewayV2EventDispatcher(
        context_repository=SimpleNamespace(),
        terminal_repository=SimpleNamespace(),
        outbox_repository=SimpleNamespace(),
        decision_planner=planner,
        hosted_chat_service=chat,
    )
    # isinstance dispatch uses the concrete contract model, so this test verifies the service seam through a real event.
    from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event

    real_event = parse_gateway_v2_event(
        {
            "eventId": f"event-{event_type}",
            "eventType": event_type,
            "sessionId": "session-1",
            "controlGeneration": 1,
            "eventSequence": 2,
            "stateVersion": 0,
            "decisionLeaseId": None,
            "occurredAtMs": 1,
            "payload": payload,
        }
    )
    await dispatcher(_claimed(event_type, real_event))
    assert chat.calls == [expected_call]
    assert planner_called is False


@pytest.mark.asyncio
async def test_retryable_chat_failure_remains_retryable_for_event_worker() -> None:
    class RetryableChatStub(ChatStub):
        async def handle_chat_received(self, gateway_id: str, event: object) -> None:
            raise HostedChatRetryableError("timeout")

    dispatcher = GatewayV2EventDispatcher(
        context_repository=SimpleNamespace(),
        terminal_repository=SimpleNamespace(),
        outbox_repository=SimpleNamespace(),
        decision_planner=SimpleNamespace(),
        hosted_chat_service=RetryableChatStub(),
    )
    from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event

    event = parse_gateway_v2_event(
        {
            "eventId": "event-chat-retry",
            "eventType": "chat_received",
            "sessionId": "session-1",
            "controlGeneration": 1,
            "eventSequence": 2,
            "stateVersion": 0,
            "decisionLeaseId": None,
            "occurredAtMs": 1,
            "payload": {
                "sessionId": "session-1",
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "supported": True,
                "text": "你好",
                "serverTimeMs": 1,
                "conversation": _conversation(),
            },
        }
    )

    result = await dispatcher(_claimed("chat_received", event))

    assert result.outcome == "retryable_failed"
    assert result.error_stage == "chat"
    assert result.error_category == "timeout"
