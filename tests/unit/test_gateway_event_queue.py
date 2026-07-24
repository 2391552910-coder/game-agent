"""Gateway Redis Stream 队列测试。"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_enqueue_gateway_event_atomically_claims_idempotency_and_stream(mock_redis, _mock_settings):
    from src.core.integration.gateway_event_queue import enqueue_gateway_event

    _mock_settings.llm_gateway_event_stream_key = "llm-gateway:test-events"
    _mock_settings.llm_gateway_idempotency_ttl_seconds = 600
    mock_redis.eval = AsyncMock(return_value=[1, "1-0"])
    record = {"traceId": "trace-001", "event": {"eventId": "evt-001"}}

    with patch("src.core.integration.gateway_event_queue.get_redis", AsyncMock(return_value=mock_redis)):
        status = await enqueue_gateway_event(
            event_id="evt-001",
            body_sha256="a" * 64,
            record=record,
        )

    assert status == "accepted"
    args = mock_redis.eval.await_args.args
    assert args[2:4] == ("llm-gateway:event:evt-001", "llm-gateway:test-events")
    assert args[4] == "a" * 64
    assert args[5] == "600"


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "expected"), [([0, ""], "duplicate"), ([-1, ""], "conflict")])
async def test_enqueue_gateway_event_maps_idempotency_result(mock_redis, _mock_settings, result, expected):
    from src.core.integration.gateway_event_queue import enqueue_gateway_event

    mock_redis.eval = AsyncMock(return_value=result)
    with patch("src.core.integration.gateway_event_queue.get_redis", AsyncMock(return_value=mock_redis)):
        status = await enqueue_gateway_event(
            event_id="evt-001",
            body_sha256="a" * 64,
            record={"event": {"eventId": "evt-001"}},
        )
    assert status == expected


@pytest.mark.asyncio
async def test_process_messages_acknowledges_only_successful_record(mock_redis):
    from src.core.integration.gateway_event_queue import _process_messages

    processor = AsyncMock()
    messages = [("1-0", {"payload": '{"event":{"eventId":"evt-001"}}'})]

    await _process_messages(mock_redis, messages, processor)

    processor.assert_awaited_once_with({"event": {"eventId": "evt-001"}})
    mock_redis.xack.assert_awaited_once_with("llm-gateway:events", "myagent2", "1-0")


@pytest.mark.asyncio
async def test_process_messages_leaves_failed_record_pending(mock_redis):
    from src.core.integration.gateway_event_queue import _process_messages

    processor = AsyncMock(side_effect=RuntimeError("agent down"))
    await _process_messages(mock_redis, [("1-0", {"payload": "{}"})], processor)

    mock_redis.xack.assert_not_awaited()
