from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.api.routes import gateway_v2
from src.core.integration.llm_gateway_v2.readiness import (
    ReadinessProbeError,
    ReadinessService,
    probe_database_readiness,
)
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry


@dataclass
class ManualClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def _ready_probe() -> None:
    return None


def _running_registry(clock: Callable[[], float]) -> WorkerStatusRegistry:
    registry = WorkerStatusRegistry(monotonic=clock)
    registry.mark_running()
    registry.heartbeat()
    return registry


def _service(
    *,
    clock: ManualClock | None = None,
    database_probe: Callable[[], Any] = _ready_probe,
    event_status: WorkerStatusRegistry | None = None,
    decision_status: WorkerStatusRegistry | None = None,
    embedding_probe: Callable[[], Any] = _ready_probe,
    rerank_probe: Callable[[], Any] = _ready_probe,
    v2_enabled: bool = True,
    embedding_enabled: bool = True,
    rerank_enabled: bool = True,
    timeout_seconds: float = 0.05,
    cache_seconds: float = 5.0,
) -> ReadinessService:
    effective_clock = clock or ManualClock()
    return ReadinessService(
        database_probe=database_probe,
        event_worker_status=event_status or _running_registry(effective_clock),
        decision_worker_status=decision_status or _running_registry(effective_clock),
        embedding_probe=embedding_probe,
        rerank_probe=rerank_probe,
        v2_enabled=v2_enabled,
        embedding_enabled=embedding_enabled,
        rerank_enabled=rerank_enabled,
        poll_interval_ms=250,
        timeout_seconds=timeout_seconds,
        cache_seconds=cache_seconds,
        monotonic=effective_clock,
        now_ms=lambda: 1_700_000_000_000,
    )


class _ScalarResult:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[str]:
        return self._values


class _Connection:
    def __init__(self, revisions: list[str]) -> None:
        self.revisions = revisions
        self.statements: list[str] = []

    async def execute(self, statement: object) -> _ScalarResult:
        sql = str(statement)
        self.statements.append(sql)
        return _ScalarResult([] if sql == "SELECT 1" else self.revisions)


@asynccontextmanager
async def _connection_factory(connection: _Connection) -> AsyncIterator[_Connection]:
    yield connection


@pytest.mark.asyncio
async def test_database_probe_rejects_missing_database_revision() -> None:
    connection = _Connection([])

    with pytest.raises(ReadinessProbeError, match="revision_mismatch"):
        await probe_database_readiness(
            connection_factory=lambda: _connection_factory(connection),
            code_head_loader=lambda: "009_llm_gateway_v2_outbox",
        )

    assert connection.statements == ["SELECT 1", "SELECT version_num FROM alembic_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_name", "check_name"),
    [("event", "eventWorker"), ("decision", "decisionWorker")],
)
async def test_stale_worker_heartbeat_is_not_ready(worker_name: str, check_name: str) -> None:
    clock = ManualClock()
    stale = _running_registry(clock)
    healthy = _running_registry(clock)
    clock.advance(2.001)

    service = _service(
        clock=clock,
        event_status=stale if worker_name == "event" else healthy,
        decision_status=stale if worker_name == "decision" else healthy,
    )
    snapshot = await service.snapshot()

    assert snapshot.status == "not_ready"
    assert snapshot.to_dict()["checks"][check_name] == {
        "status": "not_ready",
        "category": "heartbeat_stale",
        "checkedAtMs": 1_700_000_000_000,
    }


@pytest.mark.asyncio
async def test_disabled_v2_skips_workers_without_fabricating_heartbeats() -> None:
    event_status = WorkerStatusRegistry()
    decision_status = WorkerStatusRegistry()
    service = _service(
        event_status=event_status,
        decision_status=decision_status,
        v2_enabled=False,
    )

    snapshot = await service.snapshot()
    checks = snapshot.to_dict()["checks"]

    assert snapshot.status == "ready"
    assert checks["eventWorker"] == {
        "status": "disabled",
        "category": "skipped",
        "checkedAtMs": 1_700_000_000_000,
    }
    assert checks["decisionWorker"] == checks["eventWorker"]
    assert event_status.snapshot().heartbeat_monotonic is None
    assert decision_status.snapshot().heartbeat_monotonic is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency", "expected_category"),
    [("embedding", "embedding_unavailable"), ("rerank", "rerank_unavailable")],
)
async def test_enabled_model_dependency_failure_is_not_ready(
    dependency: str,
    expected_category: str,
) -> None:
    async def unavailable() -> None:
        raise RuntimeError("provider detail must not escape")

    service = _service(
        embedding_probe=unavailable if dependency == "embedding" else _ready_probe,
        rerank_probe=unavailable if dependency == "rerank" else _ready_probe,
    )

    snapshot = await service.snapshot()
    check = snapshot.to_dict()["checks"][dependency]

    assert snapshot.status == "not_ready"
    assert check["status"] == "not_ready"
    assert check["category"] == expected_category
    assert "provider detail" not in str(snapshot.to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["embedding", "rerank"])
async def test_disabled_model_dependency_is_not_called(dependency: str) -> None:
    embedding_probe = AsyncMock()
    rerank_probe = AsyncMock()
    service = _service(
        embedding_probe=embedding_probe,
        rerank_probe=rerank_probe,
        embedding_enabled=dependency != "embedding",
        rerank_enabled=dependency != "rerank",
    )

    snapshot = await service.snapshot()
    check = snapshot.to_dict()["checks"][dependency]

    assert snapshot.status == "ready"
    assert check["status"] == "disabled"
    assert check["category"] == "skipped"
    if dependency == "embedding":
        embedding_probe.assert_not_awaited()
        rerank_probe.assert_awaited_once()
    else:
        rerank_probe.assert_not_awaited()
        embedding_probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_timeout_is_reported_without_blocking_other_checks() -> None:
    blocker = asyncio.Event()

    async def blocked_probe() -> None:
        await blocker.wait()

    service = _service(embedding_probe=blocked_probe, timeout_seconds=0.01)
    snapshot = await asyncio.wait_for(service.snapshot(), timeout=0.2)

    assert snapshot.status == "not_ready"
    assert snapshot.to_dict()["checks"]["embedding"]["category"] == "timeout"


@pytest.mark.asyncio
async def test_snapshot_is_cached_until_ttl_expires() -> None:
    clock = ManualClock()
    database_probe = AsyncMock()
    service = _service(clock=clock, database_probe=database_probe, cache_seconds=5)

    first = await service.snapshot()
    clock.advance(4.999)
    second = await service.snapshot()
    clock.advance(0.002)
    third = await service.snapshot()

    assert first is second
    assert third is not second
    assert database_probe.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_snapshot_calls_share_one_probe_run() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def database_probe() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    service = _service(database_probe=database_probe, timeout_seconds=1)
    first_task = asyncio.create_task(service.snapshot())
    await entered.wait()
    second_task = asyncio.create_task(service.snapshot())
    await asyncio.sleep(0)
    release.set()

    first, second = await asyncio.gather(first_task, second_task)

    assert first is second
    assert calls == 1


@pytest.mark.asyncio
async def test_all_mandatory_checks_ready() -> None:
    snapshot = await _service().snapshot()

    assert snapshot.to_dict() == {
        "status": "ready",
        "checks": {
            "database": {
                "status": "ready",
                "category": "ok",
                "checkedAtMs": 1_700_000_000_000,
            },
            "eventWorker": {
                "status": "ready",
                "category": "ok",
                "checkedAtMs": 1_700_000_000_000,
            },
            "decisionWorker": {
                "status": "ready",
                "category": "ok",
                "checkedAtMs": 1_700_000_000_000,
            },
            "embedding": {
                "status": "ready",
                "category": "ok",
                "checkedAtMs": 1_700_000_000_000,
            },
            "rerank": {
                "status": "ready",
                "category": "ok",
                "checkedAtMs": 1_700_000_000_000,
            },
        },
    }


@pytest.fixture
def install_readiness_service():
    from src.api.main import app

    original = app.state.readiness_service

    def install(service: ReadinessService) -> None:
        app.state.readiness_service = service

    yield install
    app.state.readiness_service = original


@pytest.mark.asyncio
async def test_ready_route_returns_200_or_503_with_fixed_body(client, install_readiness_service) -> None:
    ready_service = _service()

    async def database_unavailable() -> None:
        raise RuntimeError("database detail")

    not_ready_service = _service(database_probe=database_unavailable)

    install_readiness_service(ready_service)
    ready_response = await client.get("/ready")
    install_readiness_service(not_ready_service)
    not_ready_response = await client.get("/ready")

    assert ready_response.status_code == 200
    assert ready_response.json()["status"] == "ready"
    assert list(ready_response.json()["checks"]) == [
        "database",
        "eventWorker",
        "decisionWorker",
        "embedding",
        "rerank",
    ]
    assert not_ready_response.status_code == 503
    assert not_ready_response.json()["status"] == "not_ready"
    assert not_ready_response.json()["checks"]["database"]["category"] == "database_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(("v2_enabled", "ready"), [(False, True), (True, False)])
async def test_capabilities_returns_503_when_disabled_or_not_ready(
    client,
    install_readiness_service,
    _mock_settings,
    v2_enabled: bool,
    ready: bool,
) -> None:
    async def failed_probe() -> None:
        raise RuntimeError("unavailable")

    _mock_settings.llm_gateway_v2_enabled = v2_enabled
    gateway_v2.settings = _mock_settings
    install_readiness_service(_service(database_probe=_ready_probe if ready else failed_probe))

    response = await client.get("/api/gateway/v2/capabilities")

    assert response.status_code == 503
    expected = (
        {"code": "service_disabled", "message": "service disabled"}
        if not v2_enabled
        else {"code": "service_unavailable", "message": "service unavailable"}
    )
    assert response.json() == {"error": expected}


@pytest.mark.asyncio
async def test_capabilities_returns_complete_contract_only_when_ready(
    client,
    install_readiness_service,
    _mock_settings,
) -> None:
    _mock_settings.llm_gateway_v2_enabled = True
    _mock_settings.llm_gateway_v2_max_event_batch_size = 64
    _mock_settings.llm_gateway_v2_max_decision_ttl_ms = 30_000
    gateway_v2.settings = _mock_settings
    install_readiness_service(_service())

    response = await client.get("/api/gateway/v2/capabilities")

    assert response.status_code == 200
    assert response.json()["contractVersion"] == "llm-gateway-http-v2"
    assert response.json()["receiveEventsPath"] == "/api/gateway/v2/events"
    assert response.json()["maxEventBatchSize"] == 64
    assert response.json()["maxDecisionTtlMs"] == 30_000
