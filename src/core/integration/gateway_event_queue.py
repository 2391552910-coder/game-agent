"""Gateway runtime 事件的 Redis Streams 队列和后台消费器。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from uuid import uuid4

from redis.exceptions import ResponseError

from src.core.infrastructure.redis import get_redis
from src.core.integration.llm_gateway_v2.errors import safe_exception_fields

logger = logging.getLogger(__name__)

GatewayEventProcessor = Callable[[dict[str, Any]], Awaitable[None]]

_ENQUEUE_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then
    if existing == ARGV[1] then
        return {0, ''}
    end
    return {-1, ''}
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
local stream_id = redis.call('XADD', KEYS[2], '*', 'payload', ARGV[3])
return {1, stream_id}
"""

_worker_task: asyncio.Task[None] | None = None
_worker_stop: asyncio.Event | None = None


def _settings():
    from src.config import settings

    return settings


def _stream_key() -> str:
    return str(getattr(_settings(), "llm_gateway_event_stream_key", "llm-gateway:events"))


def _consumer_group() -> str:
    return str(getattr(_settings(), "llm_gateway_event_consumer_group", "myagent2"))


async def enqueue_gateway_event(*, event_id: str, body_sha256: str, record: dict[str, Any]) -> str:
    """原子地写入事件幂等记录和 Redis Stream。"""
    redis = await get_redis()
    idempotency_key = f"llm-gateway:event:{event_id}"
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    result = await redis.eval(
        _ENQUEUE_SCRIPT,
        2,
        idempotency_key,
        _stream_key(),
        body_sha256,
        str(int(getattr(_settings(), "llm_gateway_idempotency_ttl_seconds", 86_400))),
        payload,
    )
    status = int(result[0]) if result else -1
    if status == 1:
        return "accepted"
    if status == 0:
        return "duplicate"
    if status == -1:
        return "conflict"
    raise RuntimeError(f"unexpected Gateway event queue result: {result!r}")


async def _ensure_consumer_group(redis) -> None:
    try:
        await redis.xgroup_create(
            name=_stream_key(),
            groupname=_consumer_group(),
            id="0-0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


async def _process_messages(
    redis,
    messages: list[tuple[str, dict[str, str]]],
    processor: GatewayEventProcessor,
) -> None:
    for message_id, fields in messages:
        raw_payload = fields.get("payload")
        if not raw_payload:
            logger.error("Gateway event stream message missing payload, id=%s", message_id)
            await redis.xack(_stream_key(), _consumer_group(), message_id)
            continue
        try:
            record = json.loads(raw_payload)
            if not isinstance(record, dict):
                raise ValueError("event record must be a JSON object")
            await processor(record)
        except Exception as error:
            logger.error(
                "Gateway event processing failed, stream_id=%s",
                message_id,
                extra=safe_exception_fields(
                    stage="event",
                    category="processing_failed",
                    error=error,
                ),
            )
            continue
        await redis.xack(_stream_key(), _consumer_group(), message_id)


async def _worker_loop(processor: GatewayEventProcessor, stop_event: asyncio.Event) -> None:
    redis = await get_redis()
    await _ensure_consumer_group(redis)
    consumer = _consumer_name()
    block_ms = int(getattr(_settings(), "llm_gateway_event_worker_block_ms", 1000))
    last_pending_claim_ms = 0
    retry_idle_ms = int(getattr(_settings(), "llm_gateway_event_retry_idle_ms", 30_000))

    while not stop_event.is_set():
        now_ms = int(asyncio.get_running_loop().time() * 1000)
        if now_ms - last_pending_claim_ms >= retry_idle_ms:
            last_pending_claim_ms = now_ms
            try:
                claimed = await redis.xautoclaim(
                    _stream_key(),
                    _consumer_group(),
                    consumer,
                    min_idle_time=retry_idle_ms,
                    start_id="0-0",
                    count=10,
                )
                pending_messages = claimed[1] if claimed else []
                if pending_messages:
                    await _process_messages(redis, pending_messages, processor)
            except (AttributeError, ResponseError) as error:
                logger.warning(
                    "Redis does not support pending event recovery",
                    extra=safe_exception_fields(
                        stage="event_queue",
                        category="pending_recovery_unsupported",
                        error=error,
                    ),
                )

        try:
            result = await redis.xreadgroup(
                groupname=_consumer_group(),
                consumername=consumer,
                streams={_stream_key(): ">"},
                count=10,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Gateway event stream read failed",
                extra=safe_exception_fields(
                    stage="event_queue",
                    category="stream_read_failed",
                    error=error,
                ),
            )
            await asyncio.sleep(1)
            continue

        if not result:
            continue
        messages: list[tuple[str, dict[str, str]]] = []
        for _, stream_messages in result:
            messages.extend(stream_messages)
        await _process_messages(redis, messages, processor)


async def start_gateway_event_worker(processor: GatewayEventProcessor) -> None:
    """启动每个 API 进程一个 Redis Stream consumer。"""
    global _worker_task, _worker_stop
    if not bool(getattr(_settings(), "llm_gateway_event_worker_enabled", True)):
        logger.info("Gateway event worker disabled")
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_stop = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop(processor, _worker_stop), name="gateway-event-worker")


async def stop_gateway_event_worker() -> None:
    """停止 Gateway 事件 consumer。"""
    global _worker_task, _worker_stop
    if _worker_stop is not None:
        _worker_stop.set()
    if _worker_task is not None:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
    _worker_task = None
    _worker_stop = None
