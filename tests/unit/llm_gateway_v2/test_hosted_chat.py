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
from src.core.integration.llm_gateway_v2.simple_chat import SimpleChatRoute


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


def _message(conversation: ConversationContext, content: str = "收到") -> AutoChatMessage:
    return AutoChatMessage(
        speakerRoleId=conversation.speaker_role_id,
        targetRoleId=conversation.target_role_id,
        pairKey=conversation.pair_key,
        content=content,
        summaryVersion=1,
        summaryUpdatedAt=None,
    )


@pytest.mark.asyncio
async def test_service_calls_auto_chat_and_forwards_returned_content_unchanged() -> None:
    generation_requests: list[tuple[ConversationContext, str | None]] = []

    class ConversationClient:
        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None,
        ) -> AutoChatMessage:
            generation_requests.append((conversation, latest_message))
            return AutoChatMessage(
                speakerRoleId=conversation.speaker_role_id,
                targetRoleId=conversation.target_role_id,
                pairKey=conversation.pair_key,
                content="对话端生成的原始内容",
                summaryVersion=1,
                summaryUpdatedAt=None,
            )

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest) -> HostedChatSendReceipt:
            self.requests.append(request)
            return HostedChatSendReceipt("request-1", "message-1")

    sender = Sender()
    service = HostedChatService(conversation_client=ConversationClient(), sender=sender)
    conversation = _conversation()
    event = SimpleNamespace(
        event_id="nearby-1",
        session_id="session-1",
        payload=SimpleNamespace(
            target=SimpleNamespace(avatar_id="100", role_id="200"),
            conversation=conversation,
        ),
    )

    await service.handle_nearby_friend_request("gateway-1", event)

    assert generation_requests == [(conversation, None)]
    assert len(sender.requests) == 1
    assert sender.requests[0].content == "对话端生成的原始内容"


@pytest.mark.asyncio
async def test_simple_route_forwards_deepseek_content_without_calling_auto_chat() -> None:
    class ConversationClient:
        calls = 0

        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            self.calls += 1
            raise AssertionError("simple chat must not call Auto Chat")

    class Router:
        async def route(self, text: str) -> SimpleChatRoute:
            assert text == "可以确认一下吗？"
            return SimpleChatRoute(route="simple", content="可以，已确认。")

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest, *, request_id: str | None = None):
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    conversation_client = ConversationClient()
    sender = Sender()
    service = HostedChatService(
        conversation_client=conversation_client,
        simple_router=Router(),
        sender=sender,
    )
    event = SimpleNamespace(
        event_id="simple-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="可以确认一下吗？",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=_conversation(),
        ),
    )

    await service.handle_chat_received("gateway-1", event)

    assert conversation_client.calls == 0
    assert sender.requests[0].content == "可以，已确认。"


@pytest.mark.asyncio
async def test_complex_route_calls_auto_chat_after_deepseek_classification() -> None:
    incoming_text = "  请分析并规划接下来的步骤\n"

    class ConversationClient:
        calls: list[tuple[ConversationContext, str | None]] = []

        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None,
        ) -> AutoChatMessage:
            self.calls.append((conversation, latest_message))
            return _message(conversation, "这是复杂问题的回答")

    class Router:
        async def route(self, text: str) -> SimpleChatRoute:
            assert text == incoming_text
            return SimpleChatRoute(route="complex", content="")

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest, *, request_id: str | None = None):
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-1", "message-1")

    conversation_client = ConversationClient()
    sender = Sender()
    service = HostedChatService(
        conversation_client=conversation_client,
        simple_router=Router(),
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

    assert conversation_client.calls == [(conversation, incoming_text)]
    assert sender.requests[0].content == "这是复杂问题的回答"


@pytest.mark.asyncio
async def test_three_gateway_questions_route_and_forward_as_expected() -> None:
    routes = {
        "你好": SimpleChatRoute(route="simple", content="你好，很高兴见到你。"),
        "你最近参加了什么活动？": SimpleChatRoute(route="complex", content=""),
        "你的名字叫什么": SimpleChatRoute(route="complex", content=""),
    }

    class Router:
        async def route(self, text: str) -> SimpleChatRoute:
            return routes[text]

    class ConversationClient:
        calls: list[tuple[str, str | None]] = []

        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None,
        ) -> AutoChatMessage:
            self.calls.append((conversation.conversation_id, latest_message))
            return _message(conversation, "对话端生成的复杂问题回答")

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest, *, request_id: str | None = None):
            self.requests.append(request)
            return HostedChatSendReceipt(request_id or "request-1", f"message-{len(self.requests)}")

    conversation_client = ConversationClient()
    sender = Sender()
    service = HostedChatService(
        conversation_client=conversation_client,
        simple_router=Router(),
        sender=sender,
    )

    for event_id, question in enumerate(routes, start=1):
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
        "你好，很高兴见到你。",
        "对话端生成的复杂问题回答",
        "对话端生成的复杂问题回答",
    ]
    assert conversation_client.calls == [
        ("conv-2", "你最近参加了什么活动？"),
        ("conv-3", "你的名字叫什么"),
    ]


@pytest.mark.asyncio
async def test_complex_route_uses_real_auto_chat_http_contract() -> None:
    auto_chat_requests: list[httpx.Request] = []

    async def auto_chat_handler(request: httpx.Request) -> httpx.Response:
        auto_chat_requests.append(request)
        return httpx.Response(
            200,
            json={
                "speakerRoleId": 100,
                "targetRoleId": 200,
                "pairKey": "100:200",
                "content": "我最近参加了夏日庆典。",
                "summaryVersion": 1,
                "summaryUpdatedAt": None,
            },
        )

    class Router:
        async def route(self, text: str) -> SimpleChatRoute:
            return SimpleChatRoute(route="complex", content="")

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

    conversation = _conversation(
        historyRounds=[
            {
                "askRoleId": 100,
                "askContent": "你好",
                "answerRoleId": 200,
                "answerContent": "你好呀",
            }
        ],
        completedRounds=1,
    )
    sender = Sender()
    service = HostedChatService(
        conversation_client=AutoChatClient(
            base_url="http://auto-chat.local",
            transport=httpx.MockTransport(auto_chat_handler),
        ),
        simple_router=Router(),
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
            conversation=conversation,
        ),
    )

    await service.handle_chat_received("gateway-1", event)

    assert len(auto_chat_requests) == 1
    assert auto_chat_requests[0].read().decode("utf-8") == (
        '{"conversation":{"conversationId":"conv-100-200-1",'
        '"pairKey":"100:200","speakerRoleId":100,"targetRoleId":200,'
        '"brainUsername":"conv-100","historyRounds":[{"askRoleId":100,'
        '"askContent":"你好","answerRoleId":200,"answerContent":"你好呀"}],'
        '"completedRounds":1,"maxRounds":6,"expiresAtMs":9999999999999},'
        '"latestMessage":"你最近参加了什么活动？","forceRefreshSummary":false}'
    )
    assert [request.content for request in sender.requests] == ["我最近参加了夏日庆典。"]


@pytest.mark.asyncio
async def test_hosted_chat_logs_status_without_conversation_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ConversationClient:
        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None,
        ) -> AutoChatMessage:
            return _message(conversation, "generated-secret")

    class Router:
        async def route(self, text: str) -> SimpleChatRoute:
            return SimpleChatRoute(route="complex", content="")

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
        simple_router=Router(),
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
        return httpx.Response(202, json={"accepted": True, "chatMessageId": "message-1"})

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
async def test_control_client_uses_service_supplied_request_id() -> None:
    request_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_ids.append(request.headers["X-RequestId"])
        return httpx.Response(202, json={"accepted": True, "chatMessageId": "message-1"})

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


@pytest.mark.asyncio
async def test_hosted_chat_service_ignores_unsupported_and_deduplicates_event() -> None:
    generated: list[ConversationContext] = []

    class ConversationClient:
        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            generated.append(conversation)
            return _message(conversation)

    class Sender:
        async def send(self, request: HostedChatSendRequest):
            return HostedChatSendReceipt("request-1", "message-1")

    service = HostedChatService(conversation_client=ConversationClient(), sender=Sender())
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
    assert generated == [conversation]


@pytest.mark.asyncio
async def test_auto_chat_permanent_error_does_not_send_or_regenerate_on_same_event() -> None:
    calls = 0

    class ConversationClient:
        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            nonlocal calls
            calls += 1
            raise AutoChatPermanentError("deadline_exhausted")

    class Sender:
        calls = 0

        async def send(self, request: HostedChatSendRequest):
            self.calls += 1
            return HostedChatSendReceipt("request-1", "message-1")

    sender = Sender()
    service = HostedChatService(conversation_client=ConversationClient(), sender=sender)
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
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AutoChatRetryableError("timeout")
            return _message(conversation)

    class Sender:
        calls = 0

        async def send(self, request: HostedChatSendRequest) -> HostedChatSendReceipt:
            self.calls += 1
            return HostedChatSendReceipt("request-1", "message-1")

    sender = Sender()
    service = HostedChatService(conversation_client=ConversationClient(), sender=sender)
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
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            nonlocal generator_calls
            generator_calls += 1
            return _message(conversation, "只生成一次")

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
    service = HostedChatService(conversation_client=ConversationClient(), sender=sender)
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
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            return _message(conversation)

    class Sender:
        calls = 0

        async def send(self, request: HostedChatSendRequest):
            self.calls += 1
            return HostedChatSendReceipt("request-early", "message-early")

    sender = Sender()
    service = HostedChatService(conversation_client=ConversationClient(), sender=sender)
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
            conversation: ConversationContext,
            *,
            latest_message: str | None = None,
        ) -> AutoChatMessage:
            return _message(conversation)

    class Sender:
        async def send(self, request: HostedChatSendRequest):
            return HostedChatSendReceipt("request-1", "message-1")

    service = HostedChatService(conversation_client=ConversationClient(), sender=Sender())
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


@pytest.mark.asyncio
async def test_nearby_opening_then_received_reply_uses_gateway_conversations() -> None:
    generation_requests: list[tuple[ConversationContext, str | None]] = []

    class ConversationClient:
        async def generate(
            self,
            conversation: ConversationContext,
            *,
            latest_message: str | None,
        ) -> AutoChatMessage:
            generation_requests.append((conversation, latest_message))
            content = "开场白" if len(generation_requests) == 1 else "回复内容"
            return _message(conversation, content)

    class Sender:
        requests: list[HostedChatSendRequest] = []

        async def send(self, request: HostedChatSendRequest):
            self.requests.append(request)
            sequence = len(self.requests)
            return HostedChatSendReceipt(f"request-{sequence}", f"message-{sequence}")

    sender = Sender()
    service = HostedChatService(conversation_client=ConversationClient(), sender=sender)
    opening_conversation = _conversation()
    nearby = SimpleNamespace(
        event_id="nearby-1",
        session_id="session-1",
        payload=SimpleNamespace(
            target=SimpleNamespace(avatar_id="100", role_id="200"),
            conversation=opening_conversation,
        ),
    )
    await service.handle_nearby_friend_request("gateway-1", nearby)
    sent = SimpleNamespace(
        event_id="result-1",
        payload=SimpleNamespace(
            chat_message_id="message-1",
            session_id="session-1",
            target=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            status="sent",
            reason=None,
        ),
    )
    await service.handle_send_result("gateway-1", sent)
    reply_conversation = _conversation(
        historyRounds=[
            {
                "askRoleId": 100,
                "askContent": "开场白",
                "answerRoleId": 200,
                "answerContent": "你好",
            }
        ],
        completedRounds=1,
    )
    reply = SimpleNamespace(
        event_id="received-1",
        session_id="session-1",
        payload=SimpleNamespace(
            supported=True,
            text="你好",
            sender=SimpleNamespace(avatar_id="100", role_id="200"),
            chat_type="friend",
            conversation=reply_conversation,
        ),
    )
    await service.handle_chat_received("gateway-1", reply)

    assert [request.content for request in sender.requests] == ["开场白", "回复内容"]
    assert {request.target_role_id for request in sender.requests} == {"200"}
    assert generation_requests == [
        (opening_conversation, None),
        (reply_conversation, "你好"),
    ]
    assert generation_requests[1][0].history_rounds[0].ask_content == "开场白"
