from __future__ import annotations

import time
from collections.abc import Callable
from math import isfinite
from typing import Annotated, Literal

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

PositiveRoleId = Annotated[StrictInt, Field(gt=0)]


class _AutoChatModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ConversationHistoryRound(_AutoChatModel):
    ask_role_id: PositiveRoleId = Field(alias="askRoleId")
    ask_content: str = Field(alias="askContent", min_length=1)
    answer_role_id: PositiveRoleId = Field(alias="answerRoleId")
    answer_content: str = Field(alias="answerContent", min_length=1)

    @field_validator("ask_content", "answer_content")
    @classmethod
    def validate_non_blank_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("history content must not be blank")
        return stripped


class ConversationContext(_AutoChatModel):
    conversation_id: str = Field(alias="conversationId", min_length=1, max_length=128)
    pair_key: str = Field(alias="pairKey", min_length=3, max_length=128)
    speaker_role_id: PositiveRoleId = Field(alias="speakerRoleId")
    target_role_id: PositiveRoleId = Field(alias="targetRoleId")
    brain_username: str = Field(alias="brainUsername", min_length=1, max_length=255)
    history_rounds: tuple[ConversationHistoryRound, ...] = Field(
        default=(),
        alias="historyRounds",
        max_length=5,
    )
    completed_rounds: StrictInt = Field(alias="completedRounds", ge=0)
    max_rounds: Literal[6] = Field(alias="maxRounds")
    expires_at_ms: StrictInt = Field(alias="expiresAtMs", gt=0)

    @field_validator("conversation_id", "brain_username")
    @classmethod
    def validate_non_blank_identifier(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("conversation identifier must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_pair(self) -> ConversationContext:
        if self.speaker_role_id == self.target_role_id:
            raise ValueError("conversation roles must be different")
        expected_pair_key = (
            f"{min(self.speaker_role_id, self.target_role_id)}:"
            f"{max(self.speaker_role_id, self.target_role_id)}"
        )
        if self.pair_key != expected_pair_key:
            raise ValueError("pairKey does not match conversation roles")
        if self.completed_rounds > self.max_rounds:
            raise ValueError("completedRounds must not exceed maxRounds")
        valid_roles = {self.speaker_role_id, self.target_role_id}
        for history_round in self.history_rounds:
            if {
                history_round.ask_role_id,
                history_round.answer_role_id,
            } != valid_roles:
                raise ValueError("history round roles must match conversation roles")
        return self


class AutoChatMessageRequest(_AutoChatModel):
    conversation: ConversationContext
    latest_message: str | None = Field(default=None, alias="latestMessage")
    force_refresh_summary: Literal[False] = Field(
        default=False,
        alias="forceRefreshSummary",
    )

    @classmethod
    def from_conversation(
        cls,
        conversation: ConversationContext,
        *,
        latest_message: str | None,
    ) -> AutoChatMessageRequest:
        return cls(
            conversation=conversation,
            latestMessage=latest_message,
            forceRefreshSummary=False,
        )


class AutoChatMessage(_AutoChatModel):
    speaker_role_id: PositiveRoleId = Field(alias="speakerRoleId")
    target_role_id: PositiveRoleId = Field(alias="targetRoleId")
    pair_key: str = Field(alias="pairKey", min_length=3, max_length=128)
    content: str = Field(min_length=1, max_length=80)
    summary_version: StrictInt = Field(alias="summaryVersion", ge=0)
    summary_updated_at: str | None = Field(
        validation_alias=AliasChoices("summaryUpdatedAt", "summary_updated_at"),
        serialization_alias="summaryUpdatedAt",
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped


class AutoChatError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("auto chat request failed")


class AutoChatRetryableError(AutoChatError):
    pass


class AutoChatPermanentError(AutoChatError):
    pass


class AutoChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 45.0,
        deadline_safety_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not isfinite(deadline_safety_seconds) or deadline_safety_seconds < 0:
            raise ValueError("deadline_safety_seconds must be finite and non-negative")
        self._message_url = f"{base_url.rstrip('/')}/chat/message"
        self._timeout_seconds = timeout_seconds
        self._deadline_safety_seconds = deadline_safety_seconds
        self._transport = transport
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))

    async def generate(
        self,
        conversation: ConversationContext,
        *,
        latest_message: str | None = None,
    ) -> AutoChatMessage:
        remaining_seconds = (conversation.expires_at_ms - self._now_ms()) / 1_000
        request_timeout = min(
            self._timeout_seconds,
            remaining_seconds - self._deadline_safety_seconds,
        )
        if request_timeout <= 0:
            raise AutoChatPermanentError("deadline_exhausted")

        request = AutoChatMessageRequest.from_conversation(
            conversation,
            latest_message=latest_message,
        )
        try:
            async with httpx.AsyncClient(
                timeout=request_timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._message_url,
                    json=request.model_dump(mode="json", by_alias=True),
                )
        except httpx.TimeoutException:
            raise AutoChatRetryableError("timeout") from None
        except httpx.RequestError:
            raise AutoChatRetryableError("request_failed") from None

        if response.status_code == 429 or response.status_code >= 500:
            raise AutoChatRetryableError("upstream_server_error")
        if not 200 <= response.status_code < 300:
            raise AutoChatPermanentError("upstream_request_rejected")

        try:
            payload = response.json()
        except ValueError:
            raise AutoChatPermanentError("response_not_json") from None
        try:
            message = AutoChatMessage.model_validate(payload)
        except ValidationError:
            raise AutoChatPermanentError("response_schema_invalid") from None
        if (
            message.speaker_role_id != conversation.speaker_role_id
            or message.target_role_id != conversation.target_role_id
            or message.pair_key != conversation.pair_key
        ):
            raise AutoChatPermanentError("response_identity_mismatch")
        return message
