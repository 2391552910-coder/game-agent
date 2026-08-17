from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from src.core.integration.llm_gateway_v2.token_usage import (
    GatewayV2TokenUsageCallback,
    GatewayV2TokenUsageReporter,
    GatewayV2TokenUsageTracker,
    gateway_v2_token_callback_config,
)


def _response(
    *,
    usage_metadata: dict[str, int] | None = None,
    response_metadata: dict[str, object] | None = None,
    llm_output: dict[str, object] | None = None,
) -> LLMResult:
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="ok",
                        usage_metadata=usage_metadata,
                        response_metadata=response_metadata or {},
                    )
                )
            ]
        ],
        llm_output=llm_output,
    )


async def _record_response(
    callback: GatewayV2TokenUsageCallback,
    response: LLMResult,
) -> None:
    run_id = uuid4()
    await callback.on_chat_model_start({}, [[]], run_id=run_id)
    await callback.on_llm_end(response, run_id=run_id)


async def test_callback_prefers_ai_message_usage_metadata_without_double_counting() -> None:
    tracker = GatewayV2TokenUsageTracker()
    callback = GatewayV2TokenUsageCallback()
    scope = tracker.start_decision(
        event_id="event-1",
        session_id="session-1",
        control_generation=7,
        decision_lease_id="lease-1",
    )
    try:
        await _record_response(
            callback,
            _response(
                usage_metadata={
                    "input_tokens": 101,
                    "output_tokens": 23,
                    "total_tokens": 124,
                },
                llm_output={
                    "token_usage": {
                        "prompt_tokens": 999,
                        "completion_tokens": 999,
                        "total_tokens": 1998,
                    }
                },
            ),
        )
    finally:
        decision = tracker.complete_decision(scope, decision_status="succeeded")

    assert decision.model_calls == 1
    assert decision.usage_reported_calls == 1
    assert decision.usage_missing_calls == 0
    assert decision.input_tokens == 101
    assert decision.output_tokens == 23
    assert decision.total_tokens == 124


async def test_callback_uses_llm_output_token_usage_as_fallback() -> None:
    tracker = GatewayV2TokenUsageTracker()
    callback = GatewayV2TokenUsageCallback()
    scope = tracker.start_decision(
        event_id="event-2",
        session_id="session-2",
        control_generation=8,
        decision_lease_id="lease-2",
    )
    try:
        await _record_response(
            callback,
            _response(
                llm_output={
                    "token_usage": {
                        "prompt_tokens": 44,
                        "completion_tokens": 11,
                        "total_tokens": 55,
                    }
                }
            ),
        )
    finally:
        decision = tracker.complete_decision(scope, decision_status="succeeded")

    assert decision.usage_reported_calls == 1
    assert decision.input_tokens == 44
    assert decision.output_tokens == 11
    assert decision.total_tokens == 55


async def test_callback_counts_missing_usage_and_open_timed_out_call() -> None:
    tracker = GatewayV2TokenUsageTracker()
    callback = GatewayV2TokenUsageCallback()
    scope = tracker.start_decision(
        event_id="event-3",
        session_id="session-3",
        control_generation=9,
        decision_lease_id="lease-3",
    )
    try:
        await _record_response(callback, _response())
        await callback.on_chat_model_start({}, [[]], run_id=uuid4())
    finally:
        decision = tracker.complete_decision(scope, decision_status="retryable_failed")

    assert decision.model_calls == 2
    assert decision.usage_reported_calls == 0
    assert decision.usage_missing_calls == 2
    assert decision.total_tokens == 0


async def test_multiple_model_attempts_are_aggregated_into_one_decision() -> None:
    tracker = GatewayV2TokenUsageTracker()
    callback = GatewayV2TokenUsageCallback()
    scope = tracker.start_decision(
        event_id="event-4",
        session_id="session-4",
        control_generation=10,
        decision_lease_id="lease-4",
    )
    try:
        await _record_response(
            callback,
            _response(
                usage_metadata={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}
            ),
        )
        await _record_response(
            callback,
            _response(
                usage_metadata={"input_tokens": 90, "output_tokens": 30, "total_tokens": 120}
            ),
        )
    finally:
        decision = tracker.complete_decision(scope, decision_status="succeeded")

    assert decision.model_calls == 2
    assert decision.usage_reported_calls == 2
    assert decision.input_tokens == 170
    assert decision.output_tokens == 50
    assert decision.total_tokens == 220


async def test_concurrent_decision_scopes_do_not_mix_usage() -> None:
    tracker = GatewayV2TokenUsageTracker()
    callback = GatewayV2TokenUsageCallback()

    async def decide(event_id: str, tokens: int):
        scope = tracker.start_decision(
            event_id=event_id,
            session_id=f"session-{event_id}",
            control_generation=tokens,
            decision_lease_id=f"lease-{event_id}",
        )
        try:
            await asyncio.sleep(0)
            await _record_response(
                callback,
                _response(
                    usage_metadata={
                        "input_tokens": tokens - 1,
                        "output_tokens": 1,
                        "total_tokens": tokens,
                    }
                ),
            )
            await asyncio.sleep(0)
        finally:
            completed = tracker.complete_decision(scope, decision_status="succeeded")
        return completed

    first, second = await asyncio.gather(decide("a", 100), decide("b", 200))

    assert (first.event_id, first.total_tokens) == ("a", 100)
    assert (second.event_id, second.total_tokens) == ("b", 200)
    assert tracker.snapshot_lifetime().total_tokens == 300


def test_interval_snapshot_resets_without_resetting_process_lifetime() -> None:
    tracker = GatewayV2TokenUsageTracker()
    for index, tokens in enumerate((12, 18), start=1):
        scope = tracker.start_decision(
            event_id=f"event-{index}",
            session_id="session-window",
            control_generation=1,
            decision_lease_id=f"lease-{index}",
        )
        tracker.record_model_usage(
            run_id=f"run-{index}",
            input_tokens=tokens - 2,
            output_tokens=2,
            total_tokens=tokens,
        )
        tracker.complete_decision(scope, decision_status="succeeded")

    interval = tracker.snapshot_interval(reset=True)
    empty_interval = tracker.snapshot_interval(reset=True)
    lifetime = tracker.snapshot_lifetime()

    assert interval.decision_count == 2
    assert interval.total_tokens == 30
    assert empty_interval.decision_count == 0
    assert empty_interval.total_tokens == 0
    assert lifetime.decision_count == 2
    assert lifetime.total_tokens == 30


def test_callback_config_exists_only_inside_v2_decision_scope() -> None:
    tracker = GatewayV2TokenUsageTracker()

    assert gateway_v2_token_callback_config() is None
    scope = tracker.start_decision(
        event_id="event-config",
        session_id="session-config",
        control_generation=1,
        decision_lease_id="lease-config",
    )
    try:
        config = gateway_v2_token_callback_config()
        assert config is not None
        assert len(config["callbacks"]) == 1
        assert isinstance(config["callbacks"][0], GatewayV2TokenUsageCallback)
    finally:
        tracker.complete_decision(scope, decision_status="succeeded")
    assert gateway_v2_token_callback_config() is None


async def test_reporter_logs_zero_interval_and_lifetime_summary(
    caplog,
) -> None:
    tracker = GatewayV2TokenUsageTracker()
    reporter = GatewayV2TokenUsageReporter(tracker=tracker, interval_seconds=0.01)

    with caplog.at_level(
        logging.INFO,
        logger="src.core.integration.llm_gateway_v2.token_usage",
    ):
        await reporter.start()
        await asyncio.sleep(0.03)
        await reporter.stop()

    interval_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("LLM Gateway v2 token usage interval")
    ]
    lifetime_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("LLM Gateway v2 token usage lifetime")
    ]
    assert interval_records
    assert interval_records[0].decision_count == 0
    assert interval_records[0].total_tokens == 0
    assert len(lifetime_records) == 1
    assert lifetime_records[0].decision_count == 0
    assert lifetime_records[0].total_tokens == 0
