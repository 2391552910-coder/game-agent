from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry

ReadinessStatus = Literal["ready", "not_ready"]
CheckStatus = Literal["ready", "not_ready", "disabled"]
Probe = Callable[[], Awaitable[None]]


class ReadinessProbeError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class _ScalarResult(Protocol):
    def scalars(self) -> _ScalarResult: ...

    def all(self) -> Sequence[object]: ...


class _AsyncConnection(Protocol):
    async def execute(self, statement: object) -> _ScalarResult: ...


ConnectionFactory = Callable[[], AbstractAsyncContextManager[_AsyncConnection]]


@dataclass(frozen=True)
class ReadinessCheck:
    status: CheckStatus
    category: str
    checked_at_ms: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "status": self.status,
            "category": self.category,
            "checkedAtMs": self.checked_at_ms,
        }


@dataclass(frozen=True)
class ReadinessSnapshot:
    status: ReadinessStatus
    checks: Mapping[str, ReadinessCheck]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checks": {name: check.to_dict() for name, check in self.checks.items()},
        }


def load_code_migration_head() -> str:
    project_root = Path(__file__).resolve().parents[4]
    config = Config(str(project_root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    try:
        head = script.get_current_head()
    except Exception as error:
        raise ReadinessProbeError("revision_mismatch") from error
    if head is None:
        raise ReadinessProbeError("revision_mismatch")
    return head


async def probe_database_readiness(
    *,
    connection_factory: ConnectionFactory | None = None,
    code_head_loader: Callable[[], str] = load_code_migration_head,
) -> None:
    code_head = code_head_loader()
    if connection_factory is None:
        from src.core.infrastructure.db import engine

        async with engine.connect() as connection:
            await _probe_database_connection(connection, code_head)
        return

    async with connection_factory() as connection:
        await _probe_database_connection(connection, code_head)


async def _probe_database_connection(connection: Any, code_head: str) -> None:
    await connection.execute(text("SELECT 1"))
    revision_result = await connection.execute(text("SELECT version_num FROM alembic_version"))
    database_heads = tuple(str(value) for value in revision_result.scalars().all())

    if database_heads != (code_head,):
        raise ReadinessProbeError("revision_mismatch")


async def probe_configured_embedding(
    *,
    model: str,
    api_key: str,
    base_url: str,
    embedding_dim: int,
) -> None:
    from src.core.engine.embedding import embed_texts

    vectors = await embed_texts(
        ["readiness"],
        model=model,
        api_key=api_key,
        base_url=base_url,
        embedding_dim=embedding_dim,
    )
    if tuple(getattr(vectors, "shape", ())) != (1, embedding_dim):
        raise ReadinessProbeError("embedding_invalid_response")


async def probe_configured_rerank(
    *,
    model: str,
    api_key: str,
    base_url: str,
    max_concurrency: int,
) -> None:
    from src.core.engine.rerank import is_ollama_rerank_base_url, ollama_rerank

    if is_ollama_rerank_base_url(base_url):
        results = await ollama_rerank(
            "readiness",
            ["readiness"],
            top_n=1,
            api_key=api_key or None,
            model=model,
            base_url=base_url,
            max_concurrency=max_concurrency,
        )
    else:
        from lightrag.rerank import ali_rerank  # type: ignore[import-untyped]

        results = await ali_rerank(
            "readiness",
            ["readiness"],
            top_n=1,
            api_key=api_key or None,
            model=model,
            base_url=base_url,
        )

    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        raise ReadinessProbeError("rerank_invalid_response")
    score = results[0].get("relevance_score", results[0].get("score"))
    if isinstance(score, bool) or not isinstance(score, int | float) or not math.isfinite(float(score)):
        raise ReadinessProbeError("rerank_invalid_response")


class ReadinessService:
    def __init__(
        self,
        *,
        database_probe: Probe,
        event_worker_status: WorkerStatusRegistry,
        decision_worker_status: WorkerStatusRegistry,
        embedding_probe: Probe,
        rerank_probe: Probe,
        v2_enabled: bool,
        embedding_enabled: bool,
        rerank_enabled: bool,
        poll_interval_ms: int,
        timeout_seconds: float,
        cache_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1_000),
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if cache_seconds < 0:
            raise ValueError("cache_seconds must be non-negative")
        self._database_probe = database_probe
        self._event_worker_status = event_worker_status
        self._decision_worker_status = decision_worker_status
        self._embedding_probe = embedding_probe
        self._rerank_probe = rerank_probe
        self._v2_enabled = v2_enabled
        self._embedding_enabled = embedding_enabled
        self._rerank_enabled = rerank_enabled
        self._worker_fresh_seconds = max(3 * poll_interval_ms, 2_000) / 1_000
        self._timeout_seconds = timeout_seconds
        self._cache_seconds = cache_seconds
        self._monotonic = monotonic
        self._now_ms = now_ms
        self._lock = asyncio.Lock()
        self._cached_snapshot: ReadinessSnapshot | None = None
        self._cached_at_monotonic: float | None = None
        self._cache_generation = 0
        self._enabled = True

    @property
    def v2_enabled(self) -> bool:
        return self._v2_enabled

    def invalidate(self) -> None:
        self._cache_generation += 1
        self._cached_snapshot = None
        self._cached_at_monotonic = None

    def enable(self) -> None:
        self._enabled = True
        self.invalidate()

    def disable(self) -> None:
        self._enabled = False
        self.invalidate()

    async def snapshot(self) -> ReadinessSnapshot:
        cached = self._get_cached_snapshot()
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._get_cached_snapshot()
            if cached is not None:
                return cached

            generation = self._cache_generation
            snapshot = await self._probe_snapshot()
            if generation == self._cache_generation:
                self._cached_snapshot = snapshot
                self._cached_at_monotonic = self._monotonic()
            return snapshot

    def _get_cached_snapshot(self) -> ReadinessSnapshot | None:
        if self._cached_snapshot is None or self._cached_at_monotonic is None:
            return None
        if self._monotonic() - self._cached_at_monotonic >= self._cache_seconds:
            return None
        return self._cached_snapshot

    async def _probe_snapshot(self) -> ReadinessSnapshot:
        checked_at_ms = self._now_ms()
        if not self._enabled:
            unavailable = ReadinessCheck("not_ready", "service_unavailable", checked_at_ms)
            return ReadinessSnapshot(
                status="not_ready",
                checks=MappingProxyType(
                    {
                        "database": unavailable,
                        "eventWorker": unavailable,
                        "decisionWorker": unavailable,
                        "embedding": unavailable,
                        "rerank": unavailable,
                    }
                ),
            )

        database_task = asyncio.create_task(
            self._run_probe(self._database_probe, "database_unavailable", checked_at_ms)
        )
        embedding_task = asyncio.create_task(
            self._run_optional_probe(
                self._embedding_enabled,
                self._embedding_probe,
                "embedding_unavailable",
                checked_at_ms,
            )
        )
        rerank_task = asyncio.create_task(
            self._run_optional_probe(
                self._rerank_enabled,
                self._rerank_probe,
                "rerank_unavailable",
                checked_at_ms,
            )
        )

        database, embedding, rerank = await asyncio.gather(
            database_task,
            embedding_task,
            rerank_task,
        )
        event_worker = self._check_worker(self._event_worker_status, checked_at_ms)
        decision_worker = self._check_worker(self._decision_worker_status, checked_at_ms)
        checks = MappingProxyType(
            {
                "database": database,
                "eventWorker": event_worker,
                "decisionWorker": decision_worker,
                "embedding": embedding,
                "rerank": rerank,
            }
        )
        status: ReadinessStatus = (
            "ready" if all(check.status != "not_ready" for check in checks.values()) else "not_ready"
        )
        return ReadinessSnapshot(status=status, checks=checks)

    async def _run_optional_probe(
        self,
        enabled: bool,
        probe: Probe,
        failure_category: str,
        checked_at_ms: int,
    ) -> ReadinessCheck:
        if not enabled:
            return ReadinessCheck("disabled", "skipped", checked_at_ms)
        return await self._run_probe(probe, failure_category, checked_at_ms)

    async def _run_probe(
        self,
        probe: Probe,
        failure_category: str,
        checked_at_ms: int,
    ) -> ReadinessCheck:
        try:
            await asyncio.wait_for(probe(), timeout=self._timeout_seconds)
        except TimeoutError:
            return ReadinessCheck("not_ready", "timeout", checked_at_ms)
        except ReadinessProbeError as error:
            return ReadinessCheck("not_ready", error.category, checked_at_ms)
        except Exception:
            return ReadinessCheck("not_ready", failure_category, checked_at_ms)
        return ReadinessCheck("ready", "ok", checked_at_ms)

    def _check_worker(
        self,
        registry: WorkerStatusRegistry,
        checked_at_ms: int,
    ) -> ReadinessCheck:
        if not self._v2_enabled:
            return ReadinessCheck("disabled", "skipped", checked_at_ms)

        snapshot = registry.snapshot()
        if snapshot.state != "running":
            return ReadinessCheck("not_ready", "worker_not_running", checked_at_ms)
        if snapshot.heartbeat_monotonic is None:
            return ReadinessCheck("not_ready", "heartbeat_missing", checked_at_ms)
        if self._monotonic() - snapshot.heartbeat_monotonic > self._worker_fresh_seconds:
            return ReadinessCheck("not_ready", "heartbeat_stale", checked_at_ms)
        category = "degraded" if snapshot.degraded else "ok"
        return ReadinessCheck("ready", category, checked_at_ms)


def build_readiness_service(
    *,
    event_worker_status: WorkerStatusRegistry,
    decision_worker_status: WorkerStatusRegistry,
    v2_enabled: bool,
    embedding_enabled: bool,
    rerank_enabled: bool,
    poll_interval_ms: int,
    timeout_seconds: float,
    cache_seconds: float,
    embedding_model: str,
    embedding_api_key: str,
    embedding_base_url: str,
    embedding_dim: int,
    rerank_model: str,
    rerank_api_key: str,
    rerank_base_url: str,
    rerank_max_concurrency: int,
) -> ReadinessService:
    async def embedding_probe() -> None:
        await probe_configured_embedding(
            model=embedding_model,
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            embedding_dim=embedding_dim,
        )

    async def rerank_probe() -> None:
        await probe_configured_rerank(
            model=rerank_model,
            api_key=rerank_api_key,
            base_url=rerank_base_url,
            max_concurrency=rerank_max_concurrency,
        )

    return ReadinessService(
        database_probe=probe_database_readiness,
        event_worker_status=event_worker_status,
        decision_worker_status=decision_worker_status,
        embedding_probe=embedding_probe,
        rerank_probe=rerank_probe,
        v2_enabled=v2_enabled,
        embedding_enabled=embedding_enabled,
        rerank_enabled=rerank_enabled,
        poll_interval_ms=poll_interval_ms,
        timeout_seconds=timeout_seconds,
        cache_seconds=cache_seconds,
    )
