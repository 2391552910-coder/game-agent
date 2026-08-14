from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.integration.llm_gateway_v2.contracts import parse_gateway_v2_event


def _conversation(**overrides: object) -> dict:
    conversation: dict[str, object] = {
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
    conversation.update(overrides)
    return conversation


def _event(event_type: str, payload: dict) -> dict:
    return {
        "eventId": f"chat-{event_type}",
        "eventType": event_type,
        "sessionId": "session-1",
        "stateVersion": 0,
        "decisionLeaseId": None,
        "occurredAtMs": 1,
        "payload": payload,
    }


def test_chat_received_is_parsed_without_entering_decision_lease() -> None:
    event = parse_gateway_v2_event(
        _event(
            "chat_received",
            {
                "sessionId": "session-1",
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "supported": True,
                "text": "你好",
                "serverTimeMs": 10,
                "conversation": _conversation(),
            },
        )
    )
    assert event.event_type == "chat_received"
    assert event.payload.text == "你好"
    assert event.decision_lease_id is None
    assert event.state_version == 0


def test_chat_received_accepts_gateway_v1_text_payload_without_conversation() -> None:
    event = parse_gateway_v2_event(
        _event(
            "chat_received",
            {
                "sessionId": "session-1",
                "schemaVersion": "v1",
                "contentType": 0,
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "private",
                "supported": True,
                "text": "你好",
                "serverTimeMs": 10,
            },
        )
    )

    assert event.payload.schema_version == "v1"
    assert event.payload.content_type == 0
    assert event.payload.conversation is None
    assert "controlGeneration" not in event.model_dump(mode="json")
    assert "eventSequence" not in event.model_dump(mode="json")


def test_nearby_friend_chat_requested_accepts_payload_without_conversation() -> None:
    event = parse_gateway_v2_event(
        _event(
            "nearby_friend_chat_requested",
            {
                "sessionId": "session-1",
                "schemaVersion": "v1",
                "target": {"avatarId": "100", "roleId": "200"},
                "chatType": "friend",
                "distance": 3.0,
                "friendChatCount": 0,
            },
        )
    )

    assert event.payload.schema_version == "v1"
    assert event.payload.conversation is None


def test_failed_chat_send_result_accepts_numeric_upstream_code_without_reason() -> None:
    event = parse_gateway_v2_event(
        _event(
            "chat_send_result",
            {
                "sessionId": "session-1",
                "schemaVersion": "v1",
                "chatMessageId": "message-1",
                "target": {"avatarId": "100", "roleId": "200"},
                "chatType": "private",
                "status": "failed",
                "upstreamCode": 50001,
                "completedAtMs": 10,
            },
        )
    )

    assert event.payload.schema_version == "v1"
    assert event.payload.reason is None
    assert event.payload.upstream_code == 50001


@pytest.mark.parametrize("field", ["controlGeneration", "eventSequence"])
def test_chat_received_rejects_decision_order_fields(field: str) -> None:
    event = _event(
        "chat_received",
        {
            "sessionId": "session-1",
            "schemaVersion": "v1",
            "contentType": 0,
            "sender": {"avatarId": "100", "roleId": "200"},
            "chatType": "private",
            "supported": True,
            "text": "你好",
            "serverTimeMs": 10,
        },
    )
    event[field] = 1

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(event)


def test_chat_received_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_event(
            _event(
                "chat_received",
                {
                    "sessionId": "session-1",
                    "schemaVersion": "v2",
                    "contentType": 0,
                    "sender": {"avatarId": "100", "roleId": "200"},
                    "chatType": "private",
                    "supported": True,
                    "text": "你好",
                    "serverTimeMs": 10,
                },
            )
        )


def test_supported_chat_received_rejects_non_text_content_type() -> None:
    with pytest.raises(ValidationError):
        parse_gateway_v2_event(
            _event(
                "chat_received",
                {
                    "sessionId": "session-1",
                    "schemaVersion": "v1",
                    "contentType": 1,
                    "sender": {"avatarId": "100", "roleId": "200"},
                    "chatType": "private",
                    "supported": True,
                    "text": "not-text",
                    "serverTimeMs": 10,
                },
            )
        )


@pytest.mark.parametrize("event_type", ["nearby_friend_chat_requested", "chat_send_result"])
def test_hosted_chat_events_require_zero_state_and_no_lease(event_type: str) -> None:
    payload = {
        "sessionId": "session-1",
        "schemaVersion": "v1",
        "target": {"avatarId": "100", "roleId": "200"},
        "chatType": "friend",
        "distance": 3.0,
        "friendChatCount": 0,
    }
    if event_type == "chat_send_result":
        payload = {
            "sessionId": "session-1",
            "schemaVersion": "v1",
            "chatMessageId": "message-1",
            "target": {"avatarId": "100", "roleId": "200"},
            "chatType": "friend",
            "status": "sent",
            "completedAtMs": 10,
        }
    parsed = parse_gateway_v2_event(_event(event_type, payload))
    assert parsed.state_version == 0
    assert parsed.decision_lease_id is None
    assert "controlGeneration" not in parsed.model_dump(mode="json")
    assert "eventSequence" not in parsed.model_dump(mode="json")

    invalid = _event(event_type, payload)
    invalid["stateVersion"] = 1
    with pytest.raises(ValidationError):
        parse_gateway_v2_event(invalid)

    for field in ("controlGeneration", "eventSequence"):
        invalid = _event(event_type, payload)
        invalid[field] = 1
        with pytest.raises(ValidationError):
            parse_gateway_v2_event(invalid)


def test_unsupported_chat_message_does_not_require_text() -> None:
    event = parse_gateway_v2_event(
        _event(
            "chat_received",
            {
                "sessionId": "session-1",
                "sender": {"avatarId": "100", "roleId": "200"},
                "chatType": "private",
                "supported": False,
                "serverTimeMs": 10,
                "conversation": _conversation(),
            },
        )
    )
    assert event.payload.text is None


@pytest.mark.parametrize(
    ("event_type", "party_field"),
    [
        ("chat_received", "sender"),
        ("nearby_friend_chat_requested", "target"),
    ],
)
def test_generation_event_rejects_conversation_target_mismatch(
    event_type: str,
    party_field: str,
) -> None:
    payload: dict[str, object] = {
        "sessionId": "session-1",
        party_field: {"avatarId": "100", "roleId": "200"},
        "chatType": "friend",
        "conversation": _conversation(targetRoleId=999),
    }
    if event_type == "chat_received":
        payload.update(supported=True, text="你好", serverTimeMs=10)
    else:
        payload.update(distance=3.0, friendChatCount=0)

    with pytest.raises(ValidationError):
        parse_gateway_v2_event(_event(event_type, payload))
