from __future__ import annotations

from math import isfinite
from typing import Annotated, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

PositiveRoleId = Annotated[StrictInt, Field(gt=0, le=9_223_372_036_854_775_807)]
AutoChatEventId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", strict=True),
]
_AUTO_CHAT_EVENT_ID_ADAPTER = TypeAdapter(AutoChatEventId)


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
    speaker_role_id: PositiveRoleId = Field(alias="speakerRoleId")
    target_role_id: PositiveRoleId = Field(alias="targetRoleId")
    pair_key: str = Field(alias="pairKey", min_length=5, max_length=104)
    question: str = Field(min_length=1)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_role_direction(self) -> AutoChatMessageRequest:
        if self.speaker_role_id == self.target_role_id:
            raise ValueError("speakerRoleId and targetRoleId must be different")
        return self

    @classmethod
    def from_event(
        cls,
        *,
        speaker_role_id: int,
        target_role_id: int,
        event_id: str,
        question: str,
    ) -> AutoChatMessageRequest:
        normalized_event_id = _AUTO_CHAT_EVENT_ID_ADAPTER.validate_python(event_id)
        pair_key = (
            f"{min(speaker_role_id, target_role_id)}:"
            f"{max(speaker_role_id, target_role_id)}:{normalized_event_id}"
        )
        return cls(
            speakerRoleId=speaker_role_id,
            targetRoleId=target_role_id,
            pairKey=pair_key,
            question=question,
        )


class AutoChatMessage(_AutoChatModel):
    speaker_role_id: PositiveRoleId = Field(alias="speakerRoleId")
    target_role_id: PositiveRoleId = Field(alias="targetRoleId")
    pair_key: str = Field(alias="pairKey", min_length=5, max_length=104)
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        if len(stripped.encode("utf-16-le")) // 2 > 1000:
            raise ValueError("content exceeds Gateway UTF-16 limit")
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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._message_url = _derive_message_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def generate(
        self,
        *,
        speaker_role_id: int,
        target_role_id: int,
        event_id: str,
        question: str,
    ) -> AutoChatMessage:
        try:
            request = AutoChatMessageRequest.from_event(
                speaker_role_id=speaker_role_id,
                target_role_id=target_role_id,
                event_id=event_id,
                question=question,
            )
        except ValidationError:
            raise AutoChatPermanentError("request_schema_invalid") from None
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
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
            message.speaker_role_id != request.speaker_role_id
            or message.target_role_id != request.target_role_id
            or message.pair_key != request.pair_key
        ):
            raise AutoChatPermanentError("response_identity_mismatch")
        return message


def _derive_message_url(base_url: str) -> str:
    parsed = httpx.URL(base_url.strip())
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/message"):
        path = f"{path}/chat/message" if path else "/chat/message"
    return str(parsed.copy_with(path=path, query=None, fragment=None))
