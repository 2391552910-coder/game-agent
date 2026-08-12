from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Annotated, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from src.core.integration.llm_gateway_v2.auth import build_outbound_hmac_headers
from src.core.integration.llm_gateway_v2.auto_chat import (
    AutoChatMessage,
    AutoChatPermanentError,
    AutoChatRetryableError,
    ConversationContext,
)
from src.core.integration.llm_gateway_v2.canonical import canonical_json_bytes
from src.core.integration.llm_gateway_v2.simple_chat import (
    SimpleChatPermanentError,
    SimpleChatRetryableError,
    SimpleChatRoute,
)

logger = logging.getLogger(__name__)

PositiveInt64String = Annotated[str, Field(min_length=1, max_length=19, pattern=r"[1-9][0-9]*")]
HostedChatType = Literal["friend", "private"]
HostedChatResultStatus = Literal["sent", "failed", "cancelled", "delivery_unknown"]


class _HostedChatModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)


class HostedChatSendRequest(_HostedChatModel):
    session_id: str = Field(alias="sessionId", min_length=1, max_length=128)
    target_avatar_id: PositiveInt64String = Field(alias="targetAvatarId")
    target_role_id: PositiveInt64String = Field(alias="targetRoleId")
    chat_type: HostedChatType = Field(alias="chatType")
    content: str = Field(min_length=1, max_length=1000)

    @field_validator("target_avatar_id", "target_role_id")
    @classmethod
    def validate_int64_string(cls, value: str) -> str:
        if int(value) > 9_223_372_036_854_775_807:
            raise ValueError("identifier exceeds Int64")
        return value

    @field_validator("session_id", "content")
    @classmethod
    def validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        if len(stripped.encode("utf-16-le")) // 2 > 1000:
            raise ValueError("content exceeds UTF-16 limit")
        return stripped


class HostedChatSendAccepted(_HostedChatModel):
    accepted: Literal[True]
    chat_message_id: str = Field(alias="chatMessageId", min_length=1, max_length=128)


@dataclass(frozen=True)
class HostedChatSendReceipt:
    request_id: str
    chat_message_id: str


class HostedChatPermanentError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("hosted chat operation failed permanently")


class HostedChatRetryableError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("hosted chat operation can be retried")


class HostedChatSender(Protocol):
    async def send(
        self,
        request: HostedChatSendRequest,
        *,
        request_id: str | None = None,
    ) -> HostedChatSendReceipt: ...


class HostedChatConversationClient(Protocol):
    async def generate(
        self,
        conversation: ConversationContext,
        *,
        latest_message: str | None = None,
    ) -> AutoChatMessage: ...


class HostedChatSimpleRouter(Protocol):
    async def route(self, text: str) -> SimpleChatRoute: ...


class HostedChatControlClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: SecretStr,
        timeout_seconds: float = 10.0,
        max_retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
        request_id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not base_url.strip() or not app_id.strip():
            raise ValueError("base_url and app_id must not be empty")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._url = f"{base_url.rstrip('/')}/api/v1/hosting/llm/chat/send"
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._transport = transport
        self._request_id_factory = request_id_factory or (lambda: str(uuid4()))
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))

    async def send(
        self,
        request: HostedChatSendRequest,
        *,
        request_id: str | None = None,
    ) -> HostedChatSendReceipt:
        body = canonical_json_bytes(request.model_dump(mode="json", by_alias=True))
        request_id = request_id or self._request_id_factory()
        path = httpx.URL(self._url).raw_path.decode("ascii")
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            headers = build_outbound_hmac_headers(
                method="POST",
                path=path,
                raw_body=body,
                app_id=self._app_id,
                app_secret=self._app_secret,
                request_id=request_id,
                timestamp_ms=self._now_ms(),
            )
            try:
                async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
                    response = await client.post(self._url, content=body, headers=headers)
            except httpx.TimeoutException:
                last_error = HostedChatRetryableError("timeout")
            except httpx.RequestError:
                last_error = HostedChatRetryableError("request_failed")
            else:
                if response.status_code == 202:
                    try:
                        accepted = HostedChatSendAccepted.model_validate(response.json())
                    except Exception as error:
                        raise HostedChatPermanentError("response_schema_invalid") from error
                    return HostedChatSendReceipt(request_id, accepted.chat_message_id)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = HostedChatRetryableError("upstream_server_error")
                else:
                    raise HostedChatPermanentError("upstream_request_rejected")
            if attempt < self._max_retries:
                await asyncio.sleep(0)
        assert last_error is not None
        raise last_error


@dataclass(frozen=True)
class HostedChatSendResult:
    chat_message_id: str
    session_id: str
    target_avatar_id: str
    target_role_id: str
    chat_type: HostedChatType
    status: HostedChatResultStatus
    reason: str | None


@dataclass(frozen=True)
class _ExpiringOutbound:
    request_id: str
    request: HostedChatSendRequest
    expires_at: float


@dataclass(frozen=True)
class _ExpiringResult:
    result: HostedChatSendResult
    expires_at: float


@dataclass(frozen=True)
class _ExpiringPendingSend:
    request_id: str
    request: HostedChatSendRequest
    expires_at: float


class HostedChatService:
    def __init__(
        self,
        *,
        conversation_client: HostedChatConversationClient,
        sender: HostedChatSender,
        simple_router: HostedChatSimpleRouter | None = None,
        max_queue_size: int = 100,
        state_ttl_seconds: float = 300.0,
        max_state_entries: int = 10_000,
    ) -> None:
        if (
            max_queue_size <= 0
            or not isfinite(state_ttl_seconds)
            or state_ttl_seconds <= 0
            or max_state_entries <= 0
        ):
            raise ValueError("hosted chat limits must be positive")
        self._conversation_client = conversation_client
        self._sender = sender
        self._simple_router = simple_router
        self._semaphore = asyncio.Semaphore(max_queue_size)
        self._max_queue_size = max_queue_size
        self._queued_count = 0
        self._state_ttl_seconds = state_ttl_seconds
        self._max_state_entries = max_state_entries
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._key_refcounts: dict[tuple[str, str, str], int] = {}
        self._processed_events: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._outbound: OrderedDict[str, _ExpiringOutbound] = OrderedDict()
        self._message_by_request_id: OrderedDict[str, str] = OrderedDict()
        self._pending_results: OrderedDict[str, _ExpiringResult] = OrderedDict()
        self._pending_sends: OrderedDict[tuple[str, str], _ExpiringPendingSend] = OrderedDict()
        self._state_lock = asyncio.Lock()

    async def handle_chat_received(self, gateway_id: str, event: object) -> None:
        event_id = str(event.event_id)
        if not await self._claim_event(gateway_id, event_id):
            return
        payload = event.payload
        if not payload.supported or payload.text is None:
            return
        sender = payload.sender
        try:
            await self._generate_and_send(
                gateway_id,
                event_id,
                event.session_id,
                sender.avatar_id,
                sender.role_id,
                payload.chat_type,
                payload.conversation,
                payload.text,
            )
        except HostedChatPermanentError:
            await self._discard_pending_send(gateway_id, event_id)
            raise
        except Exception:
            await self._release_event(gateway_id, event_id)
            raise

    async def handle_nearby_friend_request(self, gateway_id: str, event: object) -> None:
        event_id = str(event.event_id)
        if not await self._claim_event(gateway_id, event_id):
            return
        payload = event.payload
        target = payload.target
        try:
            await self._generate_and_send(
                gateway_id,
                event_id,
                event.session_id,
                target.avatar_id,
                target.role_id,
                "friend",
                payload.conversation,
                None,
            )
        except HostedChatPermanentError:
            await self._discard_pending_send(gateway_id, event_id)
            raise
        except Exception:
            await self._release_event(gateway_id, event_id)
            raise

    async def handle_send_result(self, gateway_id: str, event: object) -> None:
        if not await self._claim_event(gateway_id, str(event.event_id)):
            return
        payload = event.payload
        result = HostedChatSendResult(
            payload.chat_message_id,
            payload.session_id,
            payload.target.avatar_id,
            payload.target.role_id,
            payload.chat_type,
            payload.status,
            payload.reason,
        )
        async with self._state_lock:
            now = time.monotonic()
            self._expire(now)
            request = self._outbound.get(payload.chat_message_id)
            if request is None:
                self._pending_results[payload.chat_message_id] = _ExpiringResult(
                    result,
                    now + self._state_ttl_seconds,
                )
                self._pending_results.move_to_end(payload.chat_message_id)
                self._trim_state(self._pending_results)
                return
            if not self._result_matches(request.request, result):
                raise HostedChatPermanentError("result_identity_mismatch")
            self._outbound.pop(payload.chat_message_id, None)
            self._message_by_request_id.pop(request.request_id, None)
        logger.info(
            "Hosted chat send result received",
            extra={"chat_message_id": payload.chat_message_id, "status": payload.status},
        )

    async def _generate_and_send(
        self,
        gateway_id: str,
        event_id: str,
        session_id: str,
        target_avatar_id: str,
        target_role_id: str,
        chat_type: HostedChatType,
        conversation: ConversationContext,
        incoming_text: str | None,
    ) -> None:
        key = (gateway_id, session_id, conversation.pair_key)
        async with self._state_lock:
            if self._queued_count >= self._max_queue_size:
                raise HostedChatRetryableError("queue_full")
            self._queued_count += 1
            self._key_refcounts[key] = self._key_refcounts.get(key, 0) + 1
        try:
            async with self._semaphore:
                await self._generate_and_send_locked(
                    gateway_id,
                    event_id,
                    session_id,
                    target_avatar_id,
                    target_role_id,
                    chat_type,
                    conversation,
                    incoming_text,
                )
        finally:
            async with self._state_lock:
                self._queued_count -= 1
                remaining = self._key_refcounts[key] - 1
                if remaining == 0:
                    self._key_refcounts.pop(key, None)
                else:
                    self._key_refcounts[key] = remaining
                if remaining == 0:
                    self._locks.pop(key, None)

    async def _generate_and_send_locked(
        self,
        gateway_id: str,
        event_id: str,
        session_id: str,
        target_avatar_id: str,
        target_role_id: str,
        chat_type: HostedChatType,
        conversation: ConversationContext,
        incoming_text: str | None,
    ) -> None:
        key = (gateway_id, session_id, conversation.pair_key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            pending_key = (gateway_id, event_id)
            async with self._state_lock:
                pending = self._pending_sends.get(pending_key)
                if pending is not None and pending.expires_at <= time.monotonic():
                    self._pending_sends.pop(pending_key, None)
                    pending = None
            if pending is None:
                request = await self._build_request(
                    session_id=session_id,
                    target_avatar_id=target_avatar_id,
                    target_role_id=target_role_id,
                    chat_type=chat_type,
                    conversation=conversation,
                    incoming_text=incoming_text,
                )
                pending = _ExpiringPendingSend(
                    request_id=f"chat-{uuid4().hex}",
                    request=request,
                    expires_at=time.monotonic() + self._state_ttl_seconds,
                )
                async with self._state_lock:
                    self._pending_sends[pending_key] = pending
                    self._pending_sends.move_to_end(pending_key)
                    self._trim_state(self._pending_sends)
            accepted = await self._send(pending.request, pending.request_id)
            async with self._state_lock:
                now = time.monotonic()
                self._expire(now)
                self._outbound[accepted.chat_message_id] = _ExpiringOutbound(
                    accepted.request_id,
                    pending.request,
                    now + self._state_ttl_seconds,
                )
                self._outbound.move_to_end(accepted.chat_message_id)
                self._message_by_request_id[accepted.request_id] = accepted.chat_message_id
                self._message_by_request_id.move_to_end(accepted.request_id)
                self._trim_state(self._outbound)
                self._trim_state(self._message_by_request_id)
                self._pending_sends.pop(pending_key, None)
                pending_result = self._pending_results.pop(accepted.chat_message_id, None)
                if pending_result is not None:
                    if not self._result_matches(pending.request, pending_result.result):
                        self._outbound.pop(accepted.chat_message_id, None)
                        self._message_by_request_id.pop(accepted.request_id, None)
                        raise HostedChatPermanentError("result_identity_mismatch")
                    self._outbound.pop(accepted.chat_message_id, None)
                    self._message_by_request_id.pop(accepted.request_id, None)

    async def _send(self, request: HostedChatSendRequest, request_id: str) -> HostedChatSendReceipt:
        """Send with a stable request ID while tolerating legacy test doubles."""
        send = self._sender.send
        try:
            parameters = inspect.signature(send).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        supports_request_id = any(
            parameter.name == "request_id" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_request_id:
            return await send(request, request_id=request_id)
        return await send(request)

    async def _build_request(
        self,
        *,
        session_id: str,
        target_avatar_id: str,
        target_role_id: str,
        chat_type: HostedChatType,
        conversation: ConversationContext,
        incoming_text: str | None,
    ) -> HostedChatSendRequest:
        if self._simple_router is not None and incoming_text is not None:
            try:
                route = await self._simple_router.route(incoming_text)
            except SimpleChatRetryableError as error:
                raise HostedChatRetryableError(error.category) from None
            except SimpleChatPermanentError as error:
                raise HostedChatPermanentError(error.category) from None
            if route.route == "simple":
                return HostedChatSendRequest(
                    sessionId=session_id,
                    targetAvatarId=target_avatar_id,
                    targetRoleId=target_role_id,
                    chatType=chat_type,
                    content=route.content,
                )

        try:
            message = await self._conversation_client.generate(
                conversation,
                latest_message=incoming_text,
            )
        except AutoChatRetryableError as error:
            raise HostedChatRetryableError(error.category) from None
        except AutoChatPermanentError as error:
            raise HostedChatPermanentError(error.category) from None
        if (
            message.speaker_role_id != conversation.speaker_role_id
            or message.target_role_id != conversation.target_role_id
            or message.pair_key != conversation.pair_key
        ):
            raise HostedChatPermanentError("response_identity_mismatch")
        return HostedChatSendRequest(
            sessionId=session_id,
            targetAvatarId=target_avatar_id,
            targetRoleId=target_role_id,
            chatType=chat_type,
            content=message.content,
        )

    async def _claim_event(self, gateway_id: str, event_id: str) -> bool:
        now = time.monotonic()
        async with self._state_lock:
            self._expire(now)
            key = (gateway_id, event_id)
            if key in self._processed_events:
                return False
            self._processed_events[key] = now + self._state_ttl_seconds
            self._processed_events.move_to_end(key)
            self._trim_state(self._processed_events)
            return True

    async def _release_event(self, gateway_id: str, event_id: str) -> None:
        async with self._state_lock:
            self._processed_events.pop((gateway_id, event_id), None)

    async def _discard_pending_send(self, gateway_id: str, event_id: str) -> None:
        async with self._state_lock:
            self._pending_sends.pop((gateway_id, event_id), None)

    def _expire(self, now: float) -> None:
        for key, expires_at in list(self._processed_events.items()):
            if expires_at <= now:
                self._processed_events.pop(key, None)
        for message_id, outbound in list(self._outbound.items()):
            if outbound.expires_at <= now:
                self._outbound.pop(message_id, None)
                self._message_by_request_id.pop(outbound.request_id, None)
        for message_id, pending in list(self._pending_results.items()):
            if pending.expires_at <= now:
                self._pending_results.pop(message_id, None)
        for event_key, pending in list(self._pending_sends.items()):
            if pending.expires_at <= now:
                self._pending_sends.pop(event_key, None)

    def _trim_state(self, state: OrderedDict) -> None:
        while len(state) > self._max_state_entries:
            state.popitem(last=False)

    @staticmethod
    def _result_matches(request: HostedChatSendRequest, result: HostedChatSendResult) -> bool:
        return (
            request.session_id == result.session_id
            and request.target_avatar_id == result.target_avatar_id
            and request.target_role_id == result.target_role_id
            and request.chat_type == result.chat_type
        )
