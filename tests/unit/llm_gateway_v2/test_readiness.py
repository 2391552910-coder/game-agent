from __future__ import annotations

from src.core.integration.llm_gateway_v2.readiness import ReadinessService
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry


async def _probe() -> None:
    return None


def _service(
    event_status: WorkerStatusRegistry,
    decision_status: WorkerStatusRegistry,
    *,
    monotonic=lambda: 10.0,
) -> ReadinessService:
    return ReadinessService(
        database_probe=_probe,
        event_worker_status=event_status,
        decision_worker_status=decision_status,
        embedding_probe=_probe,
        rerank_probe=_probe,
        v2_enabled=True,
        embedding_enabled=True,
        rerank_enabled=True,
        poll_interval_ms=100,
        timeout_seconds=1,
        cache_seconds=5,
        monotonic=monotonic,
    )


def test_capabilities_status_only_depends_on_live_workers() -> None:
    event_status = WorkerStatusRegistry(monotonic=lambda: 10.0)
    decision_status = WorkerStatusRegistry(monotonic=lambda: 10.0)
    event_status.mark_running()
    decision_status.mark_running()
    event_status.heartbeat()
    decision_status.heartbeat()

    service = _service(event_status, decision_status)

    assert service.gateway_v2_capabilities_status() == (True, "ok")


def test_capabilities_status_does_not_report_ready_while_worker_is_stopped() -> None:
    event_status = WorkerStatusRegistry(monotonic=lambda: 10.0)
    decision_status = WorkerStatusRegistry(monotonic=lambda: 10.0)
    event_status.mark_running()
    event_status.heartbeat()

    service = _service(event_status, decision_status)

    assert service.gateway_v2_capabilities_status() == (False, "decisionWorker_not_running")


def test_capabilities_status_stays_available_when_running_worker_heartbeat_is_stale() -> None:
    event_status = WorkerStatusRegistry(monotonic=lambda: 0.0)
    decision_status = WorkerStatusRegistry(monotonic=lambda: 0.0)
    event_status.mark_running()
    decision_status.mark_running()
    event_status.heartbeat()
    decision_status.heartbeat()

    service = _service(event_status, decision_status, monotonic=lambda: 10.0)

    assert service.gateway_v2_capabilities_status() == (True, "ok")
