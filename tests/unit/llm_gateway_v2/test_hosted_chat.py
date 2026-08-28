from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from src.core.integration.llm_gateway_v2.auto_chat import (
    AutoChatClient,
    AutoChatMessage,
    AutoChatPermanentError,
    AutoChatRetryableError,
    ConversationContext,
)
from src.core.integration.llm_gateway_v2.hosted_chat import (
    HostedChatControlClient,
    HostedChatPermanentError,
    HostedChatRetryableError,
    HostedChatSendReceipt,
    HostedChatSendRequest,
    HostedChatService,
)


def _conversation(**overrides: object) -> ConversationContext:
    payload: dict[str, object] = {
        "conversationId": "conv-100-200-1",
        "pairKey": "100:200",
        "speakerRoleId": 100,
        "targetRoleId": 200,
        "brainUsername": "conv-100",
        "historyRounds": [],
        "completedRounds": 0,
        "maxRounds": 6,
        "expiresAtMs": 9_999_999_999_999,
    }
    payload.update(overrides)
    return ConversationContext.model_validate(payload)


def _message(
    speaker_role_id: int,
    target_role_id: int,
    event_id: str,
    content: str = "收到",
) -> AutoChatMessage:
    return AutoChatMessage(
        speakerRoleId=speaker_role_id,
        targetRoleId=target_role_id,
        pairKey=f"{min(speaker_role_id, target_role_id)}:{max(speaker_role_id, target_role_id)}:{event_id}",
        content=content,
    )


class _RoleResolver:
    def __init__(self, role_id: str | None = "100") -> None:
        self.role_id = role_id
        self.calls: list[tuple[str, str]] = []

    async def resolve_role_id(self, gateway_id: str, session_id: str) -> str | None:
        self.calls.append((gateway_id, session_id))
        return self.role_id


@pytest.mark.asyncio
async def test_nearby_friend_request_uses_fixed_phrase_without_auto_chat() -> None:
    phrase = "你好"

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(
            self,
            request: HostedChatSendRequest,
            *,
            request_id: str | None = None,
        ) -> HostedChatSendReceipt:
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-opening", "message-opening")

    sender = Sender()
    service = HostedChatService(
        conversation_client=None,
        identity_resolver=_RoleResolver("100"),
        sender=sender,
        opening_phrase_selector=lambda: phrase,
    )
    event = SimpleNamespace(
        event_id="opening-1",
        session_id="session-1",
        payload=SimpleNamespace(
            target=SimpleNamespace(avatar_id="300", role_id="200"),
            conversation=None,
        ),
    )

    await service.handle_nearby_friend_request("gateway-1", event)

    assert [request.content for request in sender.requests] == [phrase]


@pytest.mark.asyncio
async def test_nearby_friend_retry_reuses_first_selected_phrase() -> None:
    selected_phrases = iter(("你好", "不应再次选择"))
    selector_calls = 0

    def select_phrase() -> str:
        nonlocal selector_calls
        selector_calls += 1
        return next(selected_phrases)

    class Sender:
        requests: list[HostedChatSendRequest] = []
        calls = 0

        async def send(
            self,
            request: HostedChatSendRequest,
            *,
            request_id: str | None = None,
        ) -> HostedChatSendReceipt:
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                raise HostedChatRetryableError("upstream_server_error")
            return HostedChatSendReceipt(request_id or "request-opening", "message-opening")

    sender = Sender()
    service = HostedChatService(
        conversation_client=None,
        identity_resolver=_RoleResolver("100"),
        sender=sender,
        opening_phrase_selector=select_phrase,
    )
    event = SimpleNamespace(
        event_id="opening-retry-1",
        session_id="session-1",
        payload=SimpleNamespace(
            target=SimpleNamespace(avatar_id="300", role_id="200"),
            conversation=None,
        ),
    )

    with pytest.raises(HostedChatRetryableError):
        await service.handle_nearby_friend_request("gateway-1", event)
    await service.handle_nearby_friend_request("gateway-1", event)

    assert selector_calls == 1
    assert [request.content for request in sender.requests] == ["你好", "你好"]


@pytest.mark.asyncio
async def test_received_chat_uses_hosted_role_identity_without_gateway_conversation() -> None:
    class ConversationClient:
        calls: list[dict[str, object]] = []

        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            self.calls.append(
                {
                    "speaker_role_id": speaker_role_id,
                    "target_role_id": target_role_id,
                    "event_id": event_id,
                    "question": question,
                }
            )
            return AutoChatMessage(
                speakerRoleId=speaker_role_id,
                targetRoleId=target_role_id,
                pairKey=f"{min(speaker_role_id, target_role_id)}:"
                f"{max(speaker_role_id, target_role_id)}:{event_id}",
                content="对话端回复",
            )

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest) -> HostedChatSendReceipt:
            self.requests.append(request)
            return HostedChatSendReceipt("request-1", "message-1")

    conversation_client = ConversationClient()
    resolver = _RoleResolver("1248993658045202501")
    sender = Sender()
    service = HostedChatService(
        conversation_client=conversation_client,
        identity_resolver=resolver,
        sender=sender,
    )
    event = SimpleNamespace(
        event_id="complex-without-conversation",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你最近参加了什么活动？",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="private",
            conversation=None,
        ),
    )

    await service.handle_chat_received("gateway-1", event)

    assert resolver.calls == [("gateway-1", "session-1")]
    assert conversation_client.calls == [
        {
            "speaker_role_id": 1248993658045202501,
            "target_role_id": 200,
            "event_id": "complex-without-conversation",
            "question": "你最近参加了什么活动？",
        }
    ]
    assert [request.content for request in sender.requests] == ["对话端回复"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role_id", "category"),
    [
        (None, "hosted_role_id_missing"),
        ("not-a-number", "hosted_role_id_invalid"),
        ("0", "hosted_role_id_invalid"),
        ("9223372036854775808", "hosted_role_id_invalid"),
    ],
)
async def test_received_chat_rejects_missing_or_invalid_hosted_role_id(
    role_id: str | None,
    category: str,
) -> None:
    class ConversationClient:
        async def generate(self, **kwargs) -> AutoChatMessage:
            raise AssertionError("invalid hosted identity must not call auto chat")

    class Sender:
        async def send(self, request: HostedChatSendRequest) -> HostedChatSendReceipt:
            raise AssertionError("invalid hosted identity must not send")

    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver(role_id),
        sender=Sender(),
    )
    event = SimpleNamespace(
        event_id="invalid-hosted-role",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你最近参加了什么活动？",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="private",
            conversation=None,
        ),
    )

    with pytest.raises(HostedChatPermanentError) as raised:
        await service.handle_chat_received("gateway-1", event)

    assert raised.value.category == category


@pytest.mark.asyncio
async def test_received_chat_calls_auto_chat_with_original_question() -> None:
    incoming_text = "  请分析并规划接下来的步骤\n"

    class ConversationClient:
        calls: list[tuple[int, int, str, str]] = []

        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            self.calls.append((speaker_role_id, target_role_id, event_id, question))
            return _message(speaker_role_id, target_role_id, event_id, "这是复杂问题的回答")

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest, *, request_id: str | None = None):
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    conversation_client = ConversationClient()
    sender = Sender()
    service = HostedChatService(
        conversation_client=conversation_client,
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )
    conversation = _conversation()
    event = SimpleNamespace(
        event_id="complex-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text=incoming_text,
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=conversation,
        ),
    )

    await service.handle_chat_received("gateway-1", event)

    assert conversation_client.calls == [(100, 200, "complex-1", incoming_text)]
    assert sender.requests[0].content == "这是复杂问题的回答"


@pytest.mark.asyncio
async def test_all_gateway_questions_are_forwarded_to_auto_chat() -> None:
    questions = (
        "你好",
        "你最近参加了什么活动？",
        "你的名字叫什么",
    )
    class ConversationClient:
        calls: list[tuple[str, str]] = []

        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            self.calls.append((event_id, question))
            return _message(speaker_role_id, target_role_id, event_id, "对话端生成的回答")

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest, *, request_id: str | None = None):
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-1", f"message-{len(self.requests)}")

    conversation_client = ConversationClient()
    sender = Sender()
    service = HostedChatService(
        conversation_client=conversation_client,
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )

    for event_id, question in enumerate(questions, start=1):
        event = SimpleNamespace(
            event_id=f"question-{event_id}",
            session_id="session-1",
            payload=SimpleNamespace(
                supported=True,
                text=question,
                sender=SimpleNamespace(avatar_id="100", role_id="200"),
                chat_type="friend",
                conversation=_conversation(conversationId=f"conv-{event_id}"),
            ),
        )
        await service.handle_chat_received("gateway-1", event)

    assert [request.content for request in sender.requests] == [
        "对话端生成的回答",
        "对话端生成的回答",
        "对话端生成的回答",
    ]
    assert conversation_client.calls == [
        ("question-1", "你好"),
        ("question-2", "你最近参加了什么活动？"),
        ("question-3", "你的名字叫什么"),
    ]


@pytest.mark.asyncio
async def test_received_chat_uses_real_auto_chat_http_contract() -> None:
    auto_chat_requests: list[httpx.Request] = []

    async def auto_chat_handler(request: httpx.Request) -> httpx.Response:
        auto_chat_requests.append(request)
        return httpx.Response(
            200,
            json={
                "speakerRoleId": 100,
                "targetRoleId": 200,
                "pairKey": "100:200:complex-http-1",
                "content": "我最近参加了夏日庆典。",
            },
        )

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(
            self,
            request: HostedChatSendRequest,
            *,
            request_id: str | None = None,
        ) -> HostedChatSendReceipt:
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    sender = Sender()
    service = HostedChatService(
        conversation_client=AutoChatClient(
            base_url="http://auto-chat.local",
            transport=httpx.MockTransport(auto_chat_handler),
        ),
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )
    event = SimpleNamespace(
        event_id="complex-http-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你最近参加了什么活动？",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=None,
        ),
    )

    await service.handle_chat_received("gateway-1", event)

    assert len(auto_chat_requests) == 1
    assert auto_chat_requests[0].read().decode("utf-8") == (
        '{"speakerRoleId":100,"targetRoleId":200,'
        '"pairKey":"100:200:complex-http-1",'
        '"question":"你最近参加了什么活动？"}'
    )
    assert [request.content for request in sender.requests] == ["我最近参加了夏日庆典。"]


@pytest.mark.asyncio
async def test_hosted_chat_logs_status_without_conversation_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            return _message(speaker_role_id, target_role_id, event_id, "generated-secret")

    class Sender:
        async def send(
            self,
            request: HostedChatSendRequest,
            *,
            request_id: str | None = None,
        ) -> HostedChatSendReceipt:
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=Sender(),
    )
    conversation = _conversation(
        historyRounds=[
            {
                "askRoleId": 100,
                "askContent": "history-question-secret",
                "answerRoleId": 200,
                "answerContent": "history-answer-secret",
            }
        ],
        completedRounds=1,
    )
    received = SimpleNamespace(
        event_id="logging-chat-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="incoming-secret",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=conversation,
        ),
    )
    result = SimpleNamespace(
        event_id="logging-result-1",
        payload=SimpleNamespace(
            chat_message_id="message-1",
            session_id="session-1",
            target=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            status="sent",
            reason=None,
        ),
    )

    with caplog.at_level("INFO", logger="src.core.integration.llm_gateway_v2.hosted_chat"):
        await service.handle_chat_received("gateway-1", received)
        await service.handle_send_result("gateway-1", result)

    assert "Hosted chat send result received" in caplog.text
    for sensitive_text in (
        "incoming-secret",
        "generated-secret",
        "history-question-secret",
        "history-answer-secret",
    ):
        assert sensitive_text not in caplog.text


def test_send_request_validates_chat_contract() -> None:
    request = HostedChatSendRequest(
        sessionId="session-1",
        targetAvatarId="100",
        targetRoleId="200",
        chatType="friend",
        content="你好",
    )
    assert request.model_dump(mode="json", by_alias=True)["targetRoleId"] == "200"
    with pytest.raises(ValueError):
        HostedChatSendRequest(
            sessionId="session-1",
            targetAvatarId="100",
            targetRoleId="200",
            chatType="friend",
            content="x" * 1001,
        )


@pytest.mark.asyncio
async def test_control_client_keeps_stable_body_and_request_id_on_transport_retry() -> None:
    requests: list[httpx.Request] = []
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        requests.append(request)
        if attempts == 1:
            raise httpx.ReadError("temporary")
        return httpx.Response(
            202,
            json={
                "traceId": "trace-chat-1",
                "chatMessageId": "message-1",
                "status": "accepted",
                "acceptedAtMs": 1_000,
            },
        )

    client = HostedChatControlClient(
        base_url="http://gateway.local",
        app_id="chat-client",
        app_secret=SecretStr("secret"),
        transport=httpx.MockTransport(handler),
        request_id_factory=lambda: "request-1",
        now_ms=lambda: 1000,
        max_retries=1,
    )
    result = await client.send(
        HostedChatSendRequest(
            sessionId="session-1",
            targetAvatarId="100",
            targetRoleId="200",
            chatType="friend",
            content="你好",
        )
    )
    assert result.chat_message_id == "message-1"
    assert result.request_id == "request-1"
    assert attempts == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["X-RequestId"] == requests[1].headers["X-RequestId"] == "request-1"


@pytest.mark.asyncio
async def test_control_client_derives_chat_send_path_from_decision_url() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            202,
            json={
                "traceId": "trace-chat-2",
                "chatMessageId": "message-1",
                "status": "accepted",
                "acceptedAtMs": 1_000,
            },
        )

    client = HostedChatControlClient(
        base_url="http://gateway.local/api/v1/hosting/llm/decision",
        app_id="robot-gateway-smoke",
        app_secret=SecretStr("robot-gateway-smoke-secret"),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )

    await client.send(
        HostedChatSendRequest(
            sessionId="session-1",
            targetAvatarId="100",
            targetRoleId="200",
            chatType="friend",
            content="固定回复",
        )
    )

    assert paths == ["/api/v1/hosting/llm/chat/send"]


@pytest.mark.asyncio
async def test_control_client_uses_service_supplied_request_id() -> None:
    request_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_ids.append(request.headers["X-RequestId"])
        return httpx.Response(
            202,
            json={
                "traceId": "trace-chat-3",
                "chatMessageId": "message-1",
                "status": "accepted",
                "acceptedAtMs": 1_000,
            },
        )

    client = HostedChatControlClient(
        base_url="http://gateway.local",
        app_id="chat-client",
        app_secret=SecretStr("secret"),
        transport=httpx.MockTransport(handler),
        request_id_factory=lambda: "factory-id-must-not-be-used",
        now_ms=lambda: 1000,
        max_retries=0,
    )
    result = await client.send(
        HostedChatSendRequest(
            sessionId="session-1",
            targetAvatarId="100",
            targetRoleId="200",
            chatType="friend",
            content="你好",
        ),
        request_id="event-stable-id",
    )

    assert result.request_id == "event-stable-id"
    assert request_ids == ["event-stable-id"]


def test_send_request_preserves_content_whitespace_after_validation() -> None:
    content = "  保留首尾空格\n保留换行  "

    request = HostedChatSendRequest(
        sessionId="session-1",
        targetAvatarId="100",
        targetRoleId="200",
        chatType="friend",
        content=content,
    )

    assert request.content == content


@pytest.mark.asyncio
async def test_hosted_chat_service_ignores_unsupported_and_deduplicates_event() -> None:
    generated: list[str] = []

    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            generated.append(event_id)
            return _message(speaker_role_id, target_role_id, event_id)

    class Sender:
        async def send(self, request: HostedChatSendRequest):
            return HostedChatSendReceipt("request-1", "message-1")

    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=Sender(),
    )
    conversation = _conversation()
    unsupported = SimpleNamespace(
        event_id="event-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=False,
            text=None,
            sender=SimpleNamespace(avatar_id="1", role_id="2"),
            chat_type="friend",
            conversation=conversation,
        ),
    )
    await service.handle_chat_received("gateway-1", unsupported)
    assert generated == []

    supported = SimpleNamespace(
        event_id="event-2",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="1", role_id="2"),
            chat_type="friend",
            conversation=conversation,
        ),
    )
    await service.handle_chat_received("gateway-1", supported)
    await service.handle_chat_received("gateway-1", supported)
    assert generated == ["event-2"]


@pytest.mark.asyncio
async def test_auto_chat_permanent_error_does_not_send_or_regenerate_on_same_event() -> None:
    calls = 0

    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            nonlocal calls
            calls += 1
            raise AutoChatPermanentError("response_schema_invalid")

    class Sender:
        calls = 0

        async def send(self, request: HostedChatSendRequest):
            self.calls += 1
            return HostedChatSendReceipt("request-1", "message-1")

    sender = Sender()
    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )
    event = SimpleNamespace(
        event_id="event-timeout",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="1", role_id="2"),
            chat_type="friend",
            conversation=_conversation(),
        ),
    )
    with pytest.raises(HostedChatPermanentError, match="hosted chat operation failed permanently"):
        await service.handle_chat_received("gateway-1", event)
    await service.handle_chat_received("gateway-1", event)
    assert calls == 1
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_auto_chat_retryable_error_releases_event_for_retry() -> None:
    calls = 0

    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AutoChatRetryableError("timeout")
            return _message(speaker_role_id, target_role_id, event_id)

    class Sender:
        calls = 0

        async def send(self, request: HostedChatSendRequest) -> HostedChatSendReceipt:
            self.calls += 1
            return HostedChatSendReceipt("request-1", "message-1")

    sender = Sender()
    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )
    event = SimpleNamespace(
        event_id="event-retryable",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=_conversation(),
        ),
    )

    with pytest.raises(HostedChatRetryableError):
        await service.handle_chat_received("gateway-1", event)
    await service.handle_chat_received("gateway-1", event)

    assert calls == 2
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_sender_retry_reuses_generated_content_and_request_id() -> None:
    generator_calls = 0

    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            nonlocal generator_calls
            generator_calls += 1
            return _message(speaker_role_id, target_role_id, event_id, "只生成一次")

    class Sender:
        requests: list[HostedChatSendRequest] = []
        request_ids: list[str | None] = []
        calls = 0

        async def send(
            self,
            request: HostedChatSendRequest,
            *,
            request_id: str | None = None,
        ) -> HostedChatSendReceipt:
            self.calls += 1
            self.requests.append(request)
            self.request_ids.append(request_id)
            if self.calls == 1:
                raise HostedChatRetryableError("upstream_server_error")
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    sender = Sender()
    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )
    event = SimpleNamespace(
        event_id="event-send-retry",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=_conversation(),
        ),
    )

    with pytest.raises(HostedChatRetryableError):
        await service.handle_chat_received("gateway-1", event)
    await service.handle_chat_received("gateway-1", event)

    assert generator_calls == 1
    assert [request.content for request in sender.requests] == ["只生成一次", "只生成一次"]
    assert sender.request_ids[0] == sender.request_ids[1]


@pytest.mark.asyncio
async def test_result_before_202_is_reconciled_without_resend() -> None:
    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            return _message(speaker_role_id, target_role_id, event_id)

    class Sender:
        calls = 0

        async def send(self, request: HostedChatSendRequest):
            self.calls += 1
            return HostedChatSendReceipt("request-early", "message-early")

    sender = Sender()
    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=sender,
    )
    result_event = SimpleNamespace(
        event_id="result-1",
        payload=SimpleNamespace(
            chat_message_id="message-early",
            session_id="session-1",
            target=SimpleNamespace(avatar_id="1", role_id="2"),
            chat_type="friend",
            status="delivery_unknown",
            reason="upstream uncertain",
        ),
    )
    await service.handle_send_result("gateway-1", result_event)
    received = SimpleNamespace(
        event_id="event-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="1", role_id="2"),
            chat_type="friend",
            conversation=_conversation(),
        ),
    )
    await service.handle_chat_received("gateway-1", received)
    assert sender.calls == 1
    assert service._pending_results == {}
    assert service._outbound == {}


@pytest.mark.asyncio
async def test_send_result_rejects_mismatched_target_identity() -> None:
    class ConversationClient:
        async def generate(
            self,
            *,
            speaker_role_id: int,
            target_role_id: int,
            event_id: str,
            question: str,
        ) -> AutoChatMessage:
            return _message(speaker_role_id, target_role_id, event_id)

    class Sender:
        async def send(self, request: HostedChatSendRequest):
            return HostedChatSendReceipt("request-1", "message-1")

    service = HostedChatService(
        conversation_client=ConversationClient(),
        identity_resolver=_RoleResolver("100"),
        sender=Sender(),
    )
    received = SimpleNamespace(
        event_id="event-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="1", role_id="2"),
            chat_type="friend",
            conversation=_conversation(),
        ),
    )
    await service.handle_chat_received("gateway-1", received)
    mismatched = SimpleNamespace(
        event_id="result-1",
        payload=SimpleNamespace(
            chat_message_id="message-1",
            session_id="session-1",
            target=SimpleNamespace(avatar_id="1", role_id="999"),
            chat_type="friend",
            status="sent",
            reason=None,
        ),
    )
    with pytest.raises(HostedChatPermanentError):
        await service.handle_send_result("gateway-1", mismatched)
