"""FastAPI 应用入口。"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from src.logging_config import configure_logging

configure_logging()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from src.api.middleware import PUBLIC_PATHS, AuthMiddleware, RateLimitMiddleware  # noqa: E402
from src.api.routes import analysis, gateway_v2, providers, quota, tenants, webhooks  # noqa: E402
from src.config import settings  # noqa: E402
from src.core.infrastructure.db import close_db, init_db  # noqa: E402
from src.core.infrastructure.redis import close_redis, init_redis  # noqa: E402
from src.core.integration.gateway_event_queue import (  # noqa: E402
    start_gateway_event_worker,
    stop_gateway_event_worker,
)
from src.core.integration.llm_gateway_v2.decision_client import GatewayV2DecisionClient  # noqa: E402
from src.core.integration.llm_gateway_v2.decision_service import (  # noqa: E402
    GatewayV2DecisionPlanner,
    GatewayV2DecisionService,
)
from src.core.integration.llm_gateway_v2.decision_worker import DecisionWorker  # noqa: E402
from src.core.integration.llm_gateway_v2.errors import (  # noqa: E402
    GatewayV2OperationalError,
    safe_exception_fields,
)
from src.core.integration.llm_gateway_v2.event_service import GatewayV2EventDispatcher  # noqa: E402
from src.core.integration.llm_gateway_v2.event_worker import EventWorker  # noqa: E402
from src.core.integration.llm_gateway_v2.inbox_repository import InboxRepository  # noqa: E402
from src.core.integration.llm_gateway_v2.outbox_repository import OutboxRepository  # noqa: E402
from src.core.integration.llm_gateway_v2.readiness import (  # noqa: E402
    ReadinessService,
    build_readiness_service,
)
from src.core.integration.llm_gateway_v2.terminal_repository import TerminalRepository  # noqa: E402
from src.core.integration.llm_gateway_v2.worker_status import WorkerStatusRegistry  # noqa: E402

logger = logging.getLogger(__name__)

PUBLIC_PATHS.update({"/ready", "/api/gateway/v2/events", "/api/gateway/v2/capabilities"})

event_worker_status = WorkerStatusRegistry()
decision_worker_status = WorkerStatusRegistry()
readiness_service = build_readiness_service(
    event_worker_status=event_worker_status,
    decision_worker_status=decision_worker_status,
    v2_enabled=settings.llm_gateway_v2_enabled,
    embedding_enabled=settings.embedding_enabled,
    rerank_enabled=settings.rerank_enabled,
    poll_interval_ms=settings.llm_gateway_v2_poll_ms,
    timeout_seconds=settings.llm_gateway_v2_readiness_timeout_seconds,
    cache_seconds=settings.llm_gateway_v2_readiness_cache_seconds,
    embedding_model=settings.embedding_model,
    embedding_api_key=settings.embedding_api_key,
    embedding_base_url=settings.embedding_base_url,
    embedding_dim=settings.embedding_dim,
    rerank_model=settings.rerank_model,
    rerank_api_key=settings.rerank_api_key,
    rerank_base_url=settings.rerank_base_url,
    rerank_max_concurrency=settings.rerank_max_concurrency,
)
event_worker_status.set_state_change_callback(readiness_service.invalidate)
decision_worker_status.set_state_change_callback(readiness_service.invalidate)
readiness_service.disable()


class ManagedGatewayV2Worker(Protocol):
    async def start(self) -> None: ...

    async def drain(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True)
class GatewayV2Runtime:
    event_worker: ManagedGatewayV2Worker
    decision_worker: ManagedGatewayV2Worker


def build_gateway_v2_runtime() -> GatewayV2Runtime:
    decision_url = settings.llm_gateway_decision_url
    decision_app_id = settings.llm_gateway_decision_app_id
    decision_secret = settings.llm_gateway_decision_app_secret
    if not decision_url or not decision_app_id or not decision_secret:
        raise GatewayV2OperationalError(
            stage="configuration",
            category="decision_identity_missing",
            retryable=False,
        )

    inbox_repository = InboxRepository()
    outbox_repository = OutboxRepository()
    terminal_repository = TerminalRepository()
    decision_planner = GatewayV2DecisionPlanner(
        decision_service=GatewayV2DecisionService(),
        repository=outbox_repository,
    )
    event_dispatcher = GatewayV2EventDispatcher(
        context_repository=inbox_repository,
        terminal_repository=terminal_repository,
        outbox_repository=outbox_repository,
        decision_planner=decision_planner,
    )
    runtime_id = uuid4().hex
    event_worker = EventWorker(
        repository=inbox_repository,
        processor=event_dispatcher,
        status_registry=event_worker_status,
        worker_id=f"event-{runtime_id}",
        poll_interval_ms=settings.llm_gateway_v2_poll_ms,
        claim_ttl_ms=settings.llm_gateway_v2_claim_ttl_ms,
        max_attempts=settings.llm_gateway_v2_event_max_attempts,
        retry_base_ms=settings.llm_gateway_v2_retry_base_ms,
        retry_max_ms=settings.llm_gateway_v2_retry_max_ms,
        max_parallelism=settings.llm_gateway_v2_event_max_parallelism,
    )
    decision_client = GatewayV2DecisionClient(
        decision_url=decision_url,
        app_id=decision_app_id,
        app_secret=SecretStr(decision_secret),
        timeout_seconds=settings.llm_gateway_decision_timeout_seconds,
    )
    decision_worker = DecisionWorker(
        repository=outbox_repository,
        client=decision_client,
        status_registry=decision_worker_status,
        worker_id=f"decision-{runtime_id}",
        poll_interval_ms=settings.llm_gateway_v2_poll_ms,
        claim_ttl_ms=settings.llm_gateway_v2_claim_ttl_ms,
        max_attempts=settings.llm_gateway_v2_decision_max_attempts,
        retry_base_ms=settings.llm_gateway_v2_retry_base_ms,
        retry_max_ms=settings.llm_gateway_v2_retry_max_ms,
        max_parallelism=settings.llm_gateway_v2_decision_max_parallelism,
    )
    return GatewayV2Runtime(event_worker=event_worker, decision_worker=decision_worker)


async def _drain_worker_before_deadline(
    worker: ManagedGatewayV2Worker,
    *,
    deadline: float,
    worker_kind: str,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        await worker.stop()
        return
    try:
        await asyncio.wait_for(worker.drain(), timeout=remaining)
    except TimeoutError:
        logger.warning(
            "LLM Gateway v2 worker drain timed out",
            extra={"worker_kind": worker_kind},
        )
        await worker.stop()
    except Exception as error:
        logger.error(
            "LLM Gateway v2 worker drain failed",
            extra={
                **safe_exception_fields(
                    stage="shutdown",
                    category="worker_drain_failed",
                    error=error,
                ),
                "worker_kind": worker_kind,
            },
        )
        await worker.stop()


async def shutdown_gateway_v2_runtime(
    runtime: GatewayV2Runtime,
    *,
    grace_seconds: float,
) -> None:
    if grace_seconds <= 0:
        raise ValueError("grace_seconds must be positive")
    deadline = time.monotonic() + grace_seconds
    await _drain_worker_before_deadline(
        runtime.event_worker,
        deadline=deadline,
        worker_kind="event",
    )
    await _drain_worker_before_deadline(
        runtime.decision_worker,
        deadline=deadline,
        worker_kind="decision",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期: 启动时初始化连接, 关闭时释放资源。"""
    db_started = False
    redis_started = False
    gateway_worker_started = False
    gateway_v2_runtime: GatewayV2Runtime | None = None
    service: ReadinessService = app.state.readiness_service
    try:
        logger.info("服务启动中...")
        await init_db()
        db_started = True
        await init_redis()
        redis_started = True

        if (settings.llm_provider_source or "env").strip().lower() == "db":
            from src.core.llm.balancer import balancer

            await balancer.initialize()
        else:
            logger.info("LLM 使用 .env 配置，跳过 DB provider 预加载")
        if settings.llm_gateway_v1_enabled and settings.llm_gateway_event_worker_enabled:
            await start_gateway_event_worker(webhooks.process_gateway_event_record)
            gateway_worker_started = True
        if settings.llm_gateway_v2_enabled:
            gateway_v2_runtime = build_gateway_v2_runtime()
            app.state.gateway_v2_runtime = gateway_v2_runtime
            await gateway_v2_runtime.event_worker.start()
            await gateway_v2_runtime.decision_worker.start()
        service.enable()
        logger.info("所有服务初始化完成")
        yield
    except GatewayV2OperationalError:
        raise
    except Exception as error:
        logger.error(
            "Service dependency failed",
            extra=safe_exception_fields(
                stage="startup",
                category="dependency_unavailable",
                error=error,
            ),
        )
        raise GatewayV2OperationalError(
            stage="startup",
            category="dependency_unavailable",
            retryable=True,
        ) from None
    finally:
        service.disable()
        if gateway_v2_runtime is not None:
            await shutdown_gateway_v2_runtime(
                gateway_v2_runtime,
                grace_seconds=settings.llm_gateway_v2_shutdown_grace_seconds,
            )
            app.state.gateway_v2_runtime = None
        if gateway_worker_started:
            await stop_gateway_event_worker()
        if redis_started:
            await close_redis()
        if db_started:
            await close_db()


app = FastAPI(
    title="Game Agent Platform",
    version="2.0.0",
    description="多租户游戏玩家行为分析与预测平台",
    lifespan=lifespan,
)
app.state.readiness_service = readiness_service
app.state.gateway_v2_runtime = None

# CORS（从 .env 读取白名单，不使用通配符）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证 + 限流中间件
app.add_middleware(RateLimitMiddleware, max_requests=100, window=60)
app.add_middleware(AuthMiddleware)

# 路由注册
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(webhooks.gateway_router, prefix="/api/gateway", tags=["gateway-v1"])
app.include_router(gateway_v2.router)
app.include_router(analysis.router, prefix="/api/v1/analysis", tags=["analysis"])
app.include_router(tenants.router, prefix="/api/v1/tenants", tags=["tenants"])
app.include_router(quota.router, prefix="/api/v1/quota", tags=["quota"])
app.include_router(providers.router, prefix="/api/v1/providers", tags=["providers"])


@app.get("/health")
async def health_check() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok", "version": "2.0.0"}


@app.get("/ready")
async def readiness_check(request: Request) -> JSONResponse:
    service: ReadinessService = request.app.state.readiness_service
    snapshot = await service.snapshot()
    status_code = 200 if snapshot.status == "ready" else 503
    return JSONResponse(status_code=status_code, content=snapshot.to_dict())
