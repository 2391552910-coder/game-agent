from __future__ import annotations

import logging

from src.core.integration.llm_gateway_v2.capacity import AgentCapacityLimiter
from src.core.integration.llm_gateway_v2.runtime_metrics import (
    GatewayV2RuntimeMetrics,
    QueueMetrics,
)


def test_runtime_metrics_track_bounded_workers_queues_and_latency() -> None:
    metrics = GatewayV2RuntimeMetrics(worker_limits={"event": 32, "decision": 16})

    metrics.task_started("event")
    metrics.task_started("decision")
    metrics.set_queue("event", QueueMetrics(depth=12, oldest_age_seconds=3.5))
    metrics.set_queue("decision", QueueMetrics(depth=7, oldest_age_seconds=2.0))
    metrics.set_dead_letters("event", 1)
    metrics.record_agent_result("success", elapsed_ms=120)
    metrics.record_agent_result("timeout", elapsed_ms=55_000)
    metrics.record_callback_result("accepted", elapsed_ms=40)
    metrics.record_event_ack(elapsed_ms=12)
    metrics.record_event_admission("accepted", elapsed_ms=10)
    metrics.record_decision_superseded(2)
    metrics.record_activity_capacity_reserved("dance_auto_schedule")
    metrics.record_activity_capacity_full("dance_auto_schedule")
    metrics.task_finished("event")

    snapshot = metrics.snapshot()

    assert snapshot.worker_active == {"event": 0, "decision": 1}
    assert snapshot.worker_limit == {"event": 32, "decision": 16}
    assert snapshot.queue_depth == {"event": 12, "decision": 7}
    assert snapshot.oldest_age_seconds == {"event": 3.5, "decision": 2.0}
    assert snapshot.dead_letters == {"event": 1, "decision": 0}
    assert snapshot.agent_outcomes == {"success": 1, "timeout": 1}
    assert snapshot.agent_latency.count == 2
    assert snapshot.agent_latency.p99_ms >= 55_000
    assert snapshot.callback_latency.count == 1
    assert snapshot.event_ack_latency.count == 1
    assert snapshot.event_admission_outcomes == {"accepted": 1}
    assert snapshot.event_admission_latency.count == 1
    assert snapshot.decision_superseded_total == 2
    assert snapshot.activity_capacity_reserved == {"dance_auto_schedule": 1}
    assert snapshot.activity_capacity_full == {"dance_auto_schedule": 1}


def test_runtime_metrics_log_contains_agent_capacity_without_identifiers(caplog) -> None:
    metrics = GatewayV2RuntimeMetrics(worker_limits={"event": 32, "decision": 16})
    limiter = AgentCapacityLimiter(limit=16, acquire_timeout_seconds=0.25)

    with caplog.at_level(
        logging.INFO,
        logger="src.core.integration.llm_gateway_v2.runtime_metrics",
    ):
        metrics.log_snapshot(agent_capacity=limiter.snapshot())

    message = caplog.records[-1].getMessage()
    assert "agent_active=0" in message
    assert "agent_limit=16" in message
    assert "event_worker_limit=32" in message
    assert "gateway_event_ack_calls=0" in message
    assert "gateway_event_admission_calls=0" in message
    assert "decision_superseded_total=0" in message
    assert "activity_capacity_reserved_total=0" in message
    assert "activity_capacity_full_total=0" in message
    assert "session_id" not in message
    assert "event_id" not in message
