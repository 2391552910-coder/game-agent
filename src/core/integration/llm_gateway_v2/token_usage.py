from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from prometheus_client import REGISTRY, CollectorRegistry, Counter

logger = logging.getLogger(__name__)

_DEFAULT_REPORT_INTERVAL_SECONDS = 600.0


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class GatewayV2DecisionTokenUsage:
    event_id: str
    session_id: str
    control_generation: int
    decision_lease_id: str
    decision_status: str
    model_calls: int
    usage_reported_calls: int
    usage_missing_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    elapsed_ms: float


@dataclass(frozen=True)
class GatewayV2TokenUsageSummary:
    decision_count: int
    model_calls: int
    usage_reported_calls: int
    usage_missing_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    window_seconds: float
    process_uptime_seconds: float


@dataclass
class _ModelRun:
    completed: bool = False


@dataclass
class GatewayV2DecisionTokenScope:
    event_id: str
    session_id: str
    control_generation: int
    decision_lease_id: str
    started_at: float
    model_calls: int = 0
    usage_reported_calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    runs: dict[str, _ModelRun] = field(default_factory=dict)
    completed: bool = False
    context_token: Token[_DecisionBinding | None] | None = None


@dataclass(frozen=True)
class _DecisionBinding:
    tracker: GatewayV2TokenUsageTracker
    scope: GatewayV2DecisionTokenScope


@dataclass
class _MutableSummary:
    decision_count: int = 0
    model_calls: int = 0
    usage_reported_calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: GatewayV2DecisionTokenUsage) -> None:
        self.decision_count += 1
        self.model_calls += usage.model_calls
        self.usage_reported_calls += usage.usage_reported_calls
        self.usage_missing_calls += usage.usage_missing_calls
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens


_current_decision: ContextVar[_DecisionBinding | None] = ContextVar(
    "gateway_v2_token_usage_decision",
    default=None,
)


def _run_key(run_id: UUID | str) -> str:
    return str(run_id)


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_from_mapping(value: object) -> TokenUsage | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = _token_count(value.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _token_count(value.get("prompt_tokens"))
    output_tokens = _token_count(value.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _token_count(value.get("completion_tokens"))
    total_tokens = _token_count(value.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    normalized_input = input_tokens or 0
    normalized_output = output_tokens or 0
    normalized_total = (
        normalized_input + normalized_output
        if total_tokens is None
        else total_tokens
    )
    return TokenUsage(
        input_tokens=normalized_input,
        output_tokens=normalized_output,
        total_tokens=normalized_total,
    )


def extract_token_usage(response: LLMResult) -> TokenUsage | None:
    message_usages: list[TokenUsage] = []
    for generation_group in response.generations:
        for generation in generation_group:
            message = getattr(generation, "message", None)
            if not isinstance(message, AIMessage):
                continue
            usage = _usage_from_mapping(message.usage_metadata)
            if usage is not None:
                message_usages.append(usage)
    if message_usages:
        return TokenUsage(
            input_tokens=sum(item.input_tokens for item in message_usages),
            output_tokens=sum(item.output_tokens for item in message_usages),
            total_tokens=sum(item.total_tokens for item in message_usages),
        )

    llm_output = response.llm_output
    if isinstance(llm_output, Mapping):
        usage = _usage_from_mapping(llm_output.get("token_usage"))
        if usage is None:
            usage = _usage_from_mapping(llm_output)
        if usage is not None:
            return usage

    for generation_group in response.generations:
        for generation in generation_group:
            message = getattr(generation, "message", None)
            if not isinstance(message, AIMessage):
                continue
            usage = _usage_from_mapping(message.response_metadata.get("token_usage"))
            if usage is not None:
                return usage
    return None


class GatewayV2TokenUsageTracker:
    def __init__(self) -> None:
        self._lock = Lock()
        self._process_started_at = time.monotonic()
        self._window_started_at = self._process_started_at
        self._interval = _MutableSummary()
        self._lifetime = _MutableSummary()

    def start_decision(
        self,
        *,
        event_id: str,
        session_id: str,
        control_generation: int,
        decision_lease_id: str,
    ) -> GatewayV2DecisionTokenScope:
        scope = GatewayV2DecisionTokenScope(
            event_id=event_id,
            session_id=session_id,
            control_generation=control_generation,
            decision_lease_id=decision_lease_id,
            started_at=time.monotonic(),
        )
        scope.context_token = _current_decision.set(
            _DecisionBinding(tracker=self, scope=scope)
        )
        return scope

    def record_model_start(self, *, run_id: UUID | str) -> None:
        binding = _current_decision.get()
        if binding is None or binding.tracker is not self:
            return
        scope = binding.scope
        key = _run_key(run_id)
        with self._lock:
            if scope.completed or key in scope.runs:
                return
            scope.runs[key] = _ModelRun()
            scope.model_calls += 1

    def record_model_usage(
        self,
        *,
        run_id: UUID | str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        if any(
            _token_count(value) is None
            for value in (input_tokens, output_tokens, total_tokens)
        ):
            raise ValueError("token counts must be non-negative integers")
        binding = _current_decision.get()
        if binding is None or binding.tracker is not self:
            return
        scope = binding.scope
        key = _run_key(run_id)
        with self._lock:
            if scope.completed:
                return
            run = scope.runs.get(key)
            if run is None:
                run = _ModelRun()
                scope.runs[key] = run
                scope.model_calls += 1
            if run.completed:
                return
            run.completed = True
            scope.usage_reported_calls += 1
            scope.input_tokens += input_tokens
            scope.output_tokens += output_tokens
            scope.total_tokens += total_tokens

    def record_model_usage_missing(self, *, run_id: UUID | str) -> None:
        binding = _current_decision.get()
        if binding is None or binding.tracker is not self:
            return
        scope = binding.scope
        key = _run_key(run_id)
        with self._lock:
            if scope.completed:
                return
            run = scope.runs.get(key)
            if run is None:
                run = _ModelRun()
                scope.runs[key] = run
                scope.model_calls += 1
            if run.completed:
                return
            run.completed = True
            scope.usage_missing_calls += 1

    def complete_decision(
        self,
        scope: GatewayV2DecisionTokenScope,
        *,
        decision_status: str,
    ) -> GatewayV2DecisionTokenUsage:
        now = time.monotonic()
        with self._lock:
            if scope.completed:
                raise RuntimeError("gateway v2 token decision scope is already completed")
            for run in scope.runs.values():
                if not run.completed:
                    run.completed = True
                    scope.usage_missing_calls += 1
            scope.completed = True
            usage = GatewayV2DecisionTokenUsage(
                event_id=scope.event_id,
                session_id=scope.session_id,
                control_generation=scope.control_generation,
                decision_lease_id=scope.decision_lease_id,
                decision_status=decision_status,
                model_calls=scope.model_calls,
                usage_reported_calls=scope.usage_reported_calls,
                usage_missing_calls=scope.usage_missing_calls,
                input_tokens=scope.input_tokens,
                output_tokens=scope.output_tokens,
                total_tokens=scope.total_tokens,
                elapsed_ms=(now - scope.started_at) * 1_000,
            )
            self._interval.add(usage)
            self._lifetime.add(usage)
        if scope.context_token is not None:
            _current_decision.reset(scope.context_token)
            scope.context_token = None
        _log_decision_usage(usage)
        return usage

    def snapshot_interval(self, *, reset: bool = True) -> GatewayV2TokenUsageSummary:
        now = time.monotonic()
        with self._lock:
            summary = _summary_snapshot(
                self._interval,
                window_seconds=now - self._window_started_at,
                process_uptime_seconds=now - self._process_started_at,
            )
            if reset:
                self._interval = _MutableSummary()
                self._window_started_at = now
        return summary

    def snapshot_lifetime(self) -> GatewayV2TokenUsageSummary:
        now = time.monotonic()
        with self._lock:
            return _summary_snapshot(
                self._lifetime,
                window_seconds=now - self._process_started_at,
                process_uptime_seconds=now - self._process_started_at,
            )


def _summary_snapshot(
    source: _MutableSummary,
    *,
    window_seconds: float,
    process_uptime_seconds: float,
) -> GatewayV2TokenUsageSummary:
    return GatewayV2TokenUsageSummary(
        decision_count=source.decision_count,
        model_calls=source.model_calls,
        usage_reported_calls=source.usage_reported_calls,
        usage_missing_calls=source.usage_missing_calls,
        input_tokens=source.input_tokens,
        output_tokens=source.output_tokens,
        total_tokens=source.total_tokens,
        window_seconds=window_seconds,
        process_uptime_seconds=process_uptime_seconds,
    )


class GatewayV2TokenUsageCallback(AsyncCallbackHandler):
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        binding = _current_decision.get()
        if binding is not None:
            binding.tracker.record_model_start(run_id=run_id)

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del serialized, prompts, kwargs
        binding = _current_decision.get()
        if binding is not None:
            binding.tracker.record_model_start(run_id=run_id)

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del kwargs
        binding = _current_decision.get()
        if binding is None:
            return
        usage = extract_token_usage(response)
        if usage is None:
            binding.tracker.record_model_usage_missing(run_id=run_id)
            return
        binding.tracker.record_model_usage(
            run_id=run_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del error, kwargs
        binding = _current_decision.get()
        if binding is not None:
            binding.tracker.record_model_usage_missing(run_id=run_id)


gateway_v2_token_usage_tracker = GatewayV2TokenUsageTracker()
gateway_v2_token_usage_callback = GatewayV2TokenUsageCallback()


def gateway_v2_token_callback_config() -> dict[str, list[AsyncCallbackHandler]] | None:
    if _current_decision.get() is None:
        return None
    return {"callbacks": [gateway_v2_token_usage_callback]}


@dataclass(frozen=True)
class _LLMMetricRun:
    flow: str
    node: str
    model_type: str
    model: str


class MyAgentLLMUsageCallback(AsyncCallbackHandler):
    """Record low-cardinality LLM call and token metrics for Gateway V2."""

    def __init__(self, *, registry: CollectorRegistry = REGISTRY) -> None:
        self._calls = Counter(
            "myagent_llm_calls",
            "LLM calls by Gateway V2 flow and outcome.",
            labelnames=("flow", "model", "model_type", "node", "status"),
            registry=registry,
        )
        self._tokens = Counter(
            "myagent_llm_tokens",
            "LLM token usage by Gateway V2 flow.",
            labelnames=("direction", "flow", "model", "model_type", "node"),
            registry=registry,
        )
        self._runs: dict[str, _LLMMetricRun] = {}
        self._lock = Lock()

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del messages
        self._remember_run(
            serialized,
            run_id=run_id,
            metadata=kwargs.get("metadata"),
            invocation_params=kwargs.get("invocation_params"),
        )

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del prompts
        self._remember_run(
            serialized,
            run_id=run_id,
            metadata=kwargs.get("metadata"),
            invocation_params=kwargs.get("invocation_params"),
        )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del kwargs
        run = self._forget_run(run_id)
        usage = extract_token_usage(response)
        if run is None:
            run = _LLMMetricRun("gateway_v2", "unknown", "unknown", "unknown")
        self._calls.labels(
            flow=run.flow,
            model=run.model,
            model_type=run.model_type,
            node=run.node,
            status="success",
        ).inc()
        if usage is not None:
            labels = {
                "flow": run.flow,
                "model": run.model,
                "model_type": run.model_type,
                "node": run.node,
            }
            self._tokens.labels(direction="input", **labels).inc(usage.input_tokens)
            self._tokens.labels(direction="output", **labels).inc(usage.output_tokens)
            self._tokens.labels(direction="total", **labels).inc(usage.total_tokens)

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del error, kwargs
        run = self._forget_run(run_id)
        if run is None:
            run = _LLMMetricRun("gateway_v2", "unknown", "unknown", "unknown")
        self._calls.labels(
            flow=run.flow,
            model=run.model,
            model_type=run.model_type,
            node=run.node,
            status="error",
        ).inc()

    def _remember_run(
        self,
        serialized: Mapping[str, Any],
        *,
        run_id: UUID,
        metadata: object,
        invocation_params: object,
    ) -> None:
        metadata_map = metadata if isinstance(metadata, Mapping) else {}
        serialized_kwargs = serialized.get("kwargs")
        serialized_map = serialized_kwargs if isinstance(serialized_kwargs, Mapping) else {}
        invocation_map = invocation_params if isinstance(invocation_params, Mapping) else {}
        run = _LLMMetricRun(
            flow=_metric_label(metadata_map.get("flow"), "gateway_v2"),
            node=_metric_label(metadata_map.get("node"), "unknown"),
            model_type=_metric_label(metadata_map.get("model_type"), "unknown"),
            model=_metric_label(
                metadata_map.get("model")
                or serialized_map.get("model_name")
                or serialized_map.get("model")
                or invocation_map.get("model_name")
                or invocation_map.get("model"),
                "unknown",
            ),
        )
        with self._lock:
            self._runs.setdefault(str(run_id), run)

    def _forget_run(self, run_id: UUID) -> _LLMMetricRun | None:
        with self._lock:
            return self._runs.pop(str(run_id), None)


def _metric_label(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:128]
    return fallback


myagent_llm_metrics_registry = REGISTRY
myagent_llm_usage_callback = MyAgentLLMUsageCallback(registry=myagent_llm_metrics_registry)


def llm_call_config(
    *,
    flow: str,
    node: str,
    model_type: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one LangChain invocation config for an instrumented LLM call.

    Gateway V2 calls need both the decision-scoped token callback and the
    low-cardinality labels consumed by MyAgent metrics.  Keeping the merge in
    one place prevents a node from accidentally dropping either side when it
    passes ``config`` to ``ainvoke``.
    """
    callback_config = gateway_v2_token_callback_config() or {}
    config = dict(callback_config)
    callbacks = list(config.get("callbacks") or [])
    if myagent_llm_usage_callback not in callbacks:
        callbacks.append(myagent_llm_usage_callback)
    config["callbacks"] = callbacks

    merged_metadata = dict(config.get("metadata") or {})
    if metadata is not None:
        merged_metadata.update(metadata)
    merged_metadata.update(
        {
            "flow": flow,
            "node": node,
            "model_type": model_type,
        }
    )
    config["metadata"] = merged_metadata
    return config


class GatewayV2TokenUsageReporter:
    def __init__(
        self,
        *,
        tracker: GatewayV2TokenUsageTracker = gateway_v2_token_usage_tracker,
        interval_seconds: float = _DEFAULT_REPORT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._tracker = tracker
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(),
            name="gateway-v2-token-usage-reporter",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        _log_summary("lifetime", self._tracker.snapshot_lifetime())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            _log_summary("interval", self._tracker.snapshot_interval(reset=True))


def _log_decision_usage(usage: GatewayV2DecisionTokenUsage) -> None:
    logger.info(
        "LLM Gateway v2 token usage decision: "
        "event_id=%s session_id=%s control_generation=%d decision_lease_id=%s "
        "decision_status=%s model_calls=%d usage_reported_calls=%d "
        "usage_missing_calls=%d input_tokens=%d output_tokens=%d total_tokens=%d "
        "elapsed_ms=%.2f",
        usage.event_id,
        usage.session_id,
        usage.control_generation,
        usage.decision_lease_id,
        usage.decision_status,
        usage.model_calls,
        usage.usage_reported_calls,
        usage.usage_missing_calls,
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.elapsed_ms,
        extra=usage.__dict__,
    )


def _log_summary(kind: str, summary: GatewayV2TokenUsageSummary) -> None:
    logger.info(
        "LLM Gateway v2 token usage %s: "
        "decision_count=%d model_calls=%d usage_reported_calls=%d "
        "usage_missing_calls=%d input_tokens=%d output_tokens=%d total_tokens=%d "
        "window_seconds=%.2f process_uptime_seconds=%.2f",
        kind,
        summary.decision_count,
        summary.model_calls,
        summary.usage_reported_calls,
        summary.usage_missing_calls,
        summary.input_tokens,
        summary.output_tokens,
        summary.total_tokens,
        summary.window_seconds,
        summary.process_uptime_seconds,
        extra=summary.__dict__,
    )
